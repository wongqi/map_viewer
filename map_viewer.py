"""Read-only local 3D viewer for GridMapper multi-map projects."""

from __future__ import annotations

import argparse
import csv
import json
import math
import mimetypes
import os
import re
import shutil
import struct
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from urllib.parse import unquote, urlparse

import numpy as np
from PIL import Image


RELATIONS_HEADER = ["from_map", "to_map", "dx", "dy"]
TRANSITIONS_HEADER = [
    "transition_id", "from_map", "to_map", "world_x", "world_y",
    "world_z", "world_yaw_rad", "bidirectional", "type",
]
MAP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")


@dataclass
class MapRecord:
    map_id: str
    image: str
    resolution: float
    local_origin: list[float]
    width: int
    height: int
    root_offset: list[float]
    world_origin: list[float]
    size: list[float]
    z: float
    z_source: str


@dataclass
class RelationRecord:
    from_map: str
    to_map: str
    dx: float
    dy: float
    from_root: list[float]
    to_root: list[float]


@dataclass
class TransitionRecord:
    transition_id: str
    from_map: str
    to_map: str
    world_x: float
    world_y: float
    world_z: float
    world_yaw_rad: float
    bidirectional: bool
    type: str
    local_from: list[float]
    local_to: list[float]
    in_range: bool


@dataclass
class SceneData:
    root: Path
    maps: list[MapRecord] = field(default_factory=list)
    relations: list[RelationRecord] = field(default_factory=list)
    transitions: list[TransitionRecord] = field(default_factory=list)
    pointcloud: np.ndarray = field(default_factory=lambda: np.empty((0, 3), dtype=np.float32))
    warnings: list[str] = field(default_factory=list)
    pointcloud_error: str | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "project_path": str(self.root),
            "maps": [asdict(value) | {"image_url": f"/assets/{value.image}"} for value in self.maps],
            "relations": [asdict(value) for value in self.relations],
            "transitions": [asdict(value) for value in self.transitions],
            "warnings": self.warnings,
            "pointcloud": {
                "count": int(len(self.pointcloud)),
                "available": bool(len(self.pointcloud)),
                "error": self.pointcloud_error,
            },
        }


def _simple_yaml(path: Path) -> dict[str, object]:
    """Read the small, stable map YAML schema without requiring PyYAML."""
    result: dict[str, object] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key == "image":
            result[key] = value.strip("'\"")
        elif key == "resolution":
            result[key] = float(value)
        elif key == "origin":
            result[key] = [float(item.strip()) for item in value.strip("[]").split(",")]
    return result


def _read_csv(path: Path, expected: list[str], warnings: list[str]) -> list[dict[str, str]]:
    if not path.is_file():
        warnings.append(f"missing {path.name}")
        return []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != expected:
                warnings.append(f"invalid header in {path.name}")
                return []
            return [dict(row) for row in reader if any((value or "").strip() for value in row.values())]
    except (OSError, UnicodeError) as exc:
        warnings.append(f"cannot read {path.name}: {exc}")
        return []


def _safe_float(value: str | None, label: str, warnings: list[str]) -> float | None:
    try:
        return float(value or "")
    except ValueError:
        warnings.append(f"invalid {label}: {value!r}")
        return None


def _relation_offsets(rows: list[dict[str, str]], map_ids: set[str], warnings: list[str]) -> tuple[dict[str, tuple[float, float]], list[RelationRecord]]:
    edges: list[tuple[str, str, float, float]] = []
    for row in rows:
        source, target = row.get("from_map", ""), row.get("to_map", "")
        dx = _safe_float(row.get("dx"), "relation dx", warnings)
        dy = _safe_float(row.get("dy"), "relation dy", warnings)
        if not source or not target or dx is None or dy is None:
            warnings.append("malformed map_relations.csv row")
            continue
        if source != "ROOT" and source not in map_ids or target not in map_ids:
            warnings.append(f"map_relations.csv references unknown map: {source}->{target}")
            continue
        edges.append((source, target, dx, dy))
    if not any(source == "ROOT" for source, *_ in edges):
        warnings.append("map_relations.csv has no ROOT relation")

    offsets: dict[str, tuple[float, float]] = {"ROOT": (0.0, 0.0)}
    pending = list(edges)
    changed = True
    while pending and changed:
        changed = False
        remaining: list[tuple[str, str, float, float]] = []
        for source, target, dx, dy in pending:
            if source not in offsets:
                remaining.append((source, target, dx, dy))
                continue
            candidate = (offsets[source][0] + dx, offsets[source][1] + dy)
            if target in offsets and not np.allclose(offsets[target], candidate, atol=1e-5):
                warnings.append(f"conflicting relation for {target}")
            else:
                offsets[target] = candidate
            changed = True
        pending = remaining
    for source, target, *_ in pending:
        warnings.append(f"disconnected relation: {source}->{target}")
    for map_id in sorted(map_ids):
        if map_id not in offsets:
            warnings.append(f"map {map_id} has no ROOT offset")

    relations: list[RelationRecord] = []
    for source, target, dx, dy in edges:
        from_root = offsets.get(source, (0.0, 0.0))
        to_root = offsets.get(target, (from_root[0] + dx, from_root[1] + dy))
        relations.append(RelationRecord(source, target, dx, dy, list(from_root), list(to_root)))
    return offsets, relations


def _map_in_bounds(record: MapRecord, x: float, y: float) -> bool:
    return (
        record.world_origin[0] - 1e-6 <= x <= record.world_origin[0] + record.size[0] + 1e-6
        and record.world_origin[1] - 1e-6 <= y <= record.world_origin[1] + record.size[1] + 1e-6
    )


def _load_context_z(root: Path, warnings: list[str]) -> dict[str, float]:
    path = root / "context" / "inspection_context.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        result = {}
        for map_id, values in (data.get("maps") or {}).items():
            if isinstance(values, dict) and values.get("z_p50") is not None:
                result[str(map_id)] = float(values["z_p50"])
        return result
    except (ImportError, OSError, ValueError, TypeError) as exc:
        warnings.append(f"cannot read context height metadata: {exc}")
        return {}


def load_map_project(directory: Path, max_points: int = 150_000) -> SceneData:
    """Load a portable GridMapper project without starting ROS."""
    root = directory.expanduser().resolve()
    scene = SceneData(root=root)
    if not root.is_dir():
        scene.warnings.append(f"map directory does not exist: {root}")
        return scene

    map_ids = {path.stem for path in root.glob("*.yaml") if MAP_ID_RE.fullmatch(path.stem)}
    offsets, scene.relations = _relation_offsets(
        _read_csv(root / "map_relations.csv", RELATIONS_HEADER, scene.warnings), map_ids, scene.warnings,
    )
    context_z = _load_context_z(root, scene.warnings)
    transition_rows = _read_csv(root / "transition_points.csv", TRANSITIONS_HEADER, scene.warnings)
    transition_z: dict[str, list[float]] = {}
    parsed_transitions: list[dict[str, object]] = []
    for row in transition_rows:
        source, target = row.get("from_map", ""), row.get("to_map", "")
        values = [_safe_float(row.get(key), key, scene.warnings) for key in ("world_x", "world_y", "world_z", "world_yaw_rad")]
        if source not in map_ids or target not in map_ids or any(value is None for value in values):
            scene.warnings.append(f"malformed transition row: {row.get('transition_id', '')}")
            continue
        world_x, world_y, world_z, yaw = (float(value) for value in values)
        transition_z.setdefault(source, []).append(world_z)
        transition_z.setdefault(target, []).append(world_z)
        parsed_transitions.append({
            "row": row, "source": source, "target": target,
            "world_x": world_x, "world_y": world_y, "world_z": world_z, "yaw": yaw,
        })

    z_values = dict(context_z)
    z_sources = {map_id: "context" for map_id in context_z}
    for map_id, values in transition_z.items():
        if map_id not in z_values and values:
            z_values[map_id] = float(median(values))
            z_sources[map_id] = "estimated_from_transitions"
    # Propagate known heights across the relation graph for maps without direct samples.
    for _ in range(len(map_ids) + 1):
        changed = False
        for relation in scene.relations:
            if relation.from_map in z_values and relation.to_map not in z_values:
                z_values[relation.to_map] = z_values[relation.from_map]
                z_sources[relation.to_map] = "propagated"
                changed = True
            elif relation.to_map in z_values and relation.from_map not in z_values and relation.from_map != "ROOT":
                z_values[relation.from_map] = z_values[relation.to_map]
                z_sources[relation.from_map] = "propagated"
                changed = True
        if not changed:
            break

    for map_id in sorted(map_ids):
        yaml_path = root / f"{map_id}.yaml"
        try:
            config = _simple_yaml(yaml_path)
            image_name = str(config["image"])
            resolution = float(config["resolution"])
            origin = [float(value) for value in config["origin"]]  # type: ignore[arg-type]
            image_path = root / image_name
            with Image.open(image_path) as image:
                width, height = image.size
        except (KeyError, OSError, ValueError, TypeError) as exc:
            scene.warnings.append(f"cannot load {map_id}: {exc}")
            continue
        root_offset = offsets.get(map_id, (0.0, 0.0))
        z = z_values.get(map_id, 0.0)
        source = z_sources.get(map_id, "default_zero")
        if source == "default_zero":
            scene.warnings.append(f"map {map_id} height defaults to z=0")
        scene.maps.append(MapRecord(
            map_id, image_name, resolution, origin, width, height,
            list(root_offset), [root_offset[0] + origin[0], root_offset[1] + origin[1], z],
            [width * resolution, height * resolution], z, source,
        ))
    by_id = {record.map_id: record for record in scene.maps}
    for item in parsed_transitions:
        source, target = str(item["source"]), str(item["target"])
        world_x, world_y = float(item["world_x"]), float(item["world_y"])
        source_offset = offsets.get(source, (0.0, 0.0))
        target_offset = offsets.get(target, (0.0, 0.0))
        local_from = [world_x - source_offset[0], world_y - source_offset[1]]
        local_to = [world_x - target_offset[0], world_y - target_offset[1]]
        in_range = source in by_id and target in by_id and _map_in_bounds(by_id[source], world_x, world_y) and _map_in_bounds(by_id[target], world_x, world_y)
        if not in_range:
            scene.warnings.append(f"transition {item['row'].get('transition_id', '')} is outside a map boundary")
        row = item["row"]
        scene.transitions.append(TransitionRecord(
            str(row.get("transition_id", "")), source, target, world_x, world_y,
            float(item["world_z"]), float(item["yaw"]), str(row.get("bidirectional", "")).lower() == "true",
            str(row.get("type", "manual")), local_from, local_to, in_range,
        ))
    pcd = root / "PGO.pcd"
    if pcd.is_file():
        try:
            scene.pointcloud = load_pcd_xyz(pcd, max_points=max_points)
        except (OSError, ValueError, struct.error) as exc:
            scene.pointcloud_error = str(exc)
            scene.warnings.append(f"PGO.pcd unavailable: {exc}")
    else:
        scene.pointcloud_error = "PGO.pcd is missing"
        scene.warnings.append("missing PGO.pcd")
    return scene


def _normalise_bool(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return "true"
    if text in {"false", "0", "no"}:
        return "false"
    raise ValueError(f"invalid bidirectional value: {value!r}")


def save_transition_rows(root: Path, rows: list[dict[str, object]]) -> None:
    """Validate and atomically replace transition_points.csv, keeping a backup."""
    root = root.expanduser().resolve()
    map_ids = {path.stem for path in root.glob("*.yaml") if MAP_ID_RE.fullmatch(path.stem)}
    if not map_ids:
        raise ValueError("no map YAML files found")
    normalised: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            raise ValueError(f"transition {index} is not an object")
        values = {key: str(item.get(key, "")).strip() for key in TRANSITIONS_HEADER}
        transition_id = values["transition_id"]
        if not transition_id or transition_id in seen_ids or any(char in transition_id for char in ",\r\n"):
            raise ValueError(f"invalid or duplicate transition_id: {transition_id!r}")
        if values["from_map"] not in map_ids or values["to_map"] not in map_ids:
            raise ValueError(f"transition {transition_id} references an unknown map")
        if values["from_map"] == values["to_map"]:
            raise ValueError(f"transition {transition_id} must connect two different maps")
        for key in ("world_x", "world_y", "world_z", "world_yaw_rad"):
            try:
                number = float(values[key])
            except ValueError as exc:
                raise ValueError(f"transition {transition_id} has invalid {key}") from exc
            if not math.isfinite(number):
                raise ValueError(f"transition {transition_id} has non-finite {key}")
            values[key] = format(number, ".9g")
        values["bidirectional"] = _normalise_bool(values["bidirectional"])
        values["type"] = values["type"] or "manual"
        seen_ids.add(transition_id)
        normalised.append(values)
    path = root / "transition_points.csv"
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".transition_points.", suffix=".csv", dir=str(root), text=True)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRANSITIONS_HEADER, lineterminator="\n")
            writer.writeheader()
            writer.writerows(normalised)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _lzf_decompress(data: bytes, expected_size: int) -> bytes:
    output = bytearray()
    index = 0
    while index < len(data):
        control = data[index]
        index += 1
        if control < 32:
            length = control + 1
            if index + length > len(data):
                raise ValueError("truncated LZF literal run")
            output.extend(data[index:index + length])
            index += length
            continue
        length = control >> 5
        reference = (control & 0x1F) << 8
        if length == 7:
            if index >= len(data):
                raise ValueError("truncated LZF length")
            length += data[index]
            index += 1
        if index >= len(data):
            raise ValueError("truncated LZF back reference")
        reference += data[index]
        index += 1
        length += 2
        reference += 1
        if reference > len(output):
            raise ValueError("invalid LZF back reference")
        source_index = len(output) - reference
        for copy_index in range(length):
            output.append(output[source_index + copy_index])
    if len(output) != expected_size:
        raise ValueError(f"LZF size mismatch: expected {expected_size}, got {len(output)}")
    return bytes(output)


_PCD_DTYPES = {
    ("F", 4): "<f4", ("F", 8): "<f8",
    ("I", 1): "<i1", ("I", 2): "<i2", ("I", 4): "<i4", ("I", 8): "<i8",
    ("U", 1): "<u1", ("U", 2): "<u2", ("U", 4): "<u4", ("U", 8): "<u8",
}


def _pcd_header(path: Path) -> tuple[dict[str, list[str]], int]:
    header_lines: list[str] = []
    with path.open("rb") as handle:
        while True:
            raw = handle.readline()
            if not raw:
                raise ValueError("PCD header is incomplete")
            line = raw.decode("ascii", errors="strict").strip()
            header_lines.append(line)
            if line.upper().startswith("DATA "):
                offset = handle.tell()
                break
    header: dict[str, list[str]] = {}
    for line in header_lines:
        parts = line.split()
        if len(parts) >= 2:
            header[parts[0].upper()] = parts[1:]
    return header, offset


def load_pcd_xyz(path: Path, max_points: int = 150_000) -> np.ndarray:
    """Load XYZ from ASCII, binary, or PCL binary_compressed PCD."""
    header, offset = _pcd_header(path)
    mode = header.get("DATA", [""])[0].lower()
    fields = header.get("FIELDS", [])
    sizes = [int(value) for value in header.get("SIZE", [])]
    types = header.get("TYPE", [])
    counts = [int(value) for value in header.get("COUNT", ["1"] * len(fields))]
    points = int(header.get("POINTS", header.get("WIDTH", ["0"]))[0])
    if not fields or len(fields) != len(sizes) or len(fields) != len(types) or len(fields) != len(counts):
        raise ValueError("PCD field metadata is invalid")
    if not {"x", "y", "z"}.issubset(fields):
        raise ValueError("PCD does not contain x/y/z fields")
    payload = path.read_bytes()[offset:]
    xyz: np.ndarray
    if mode == "ascii":
        rows = []
        indices = [fields.index(axis) for axis in ("x", "y", "z")]
        for line in payload.decode("ascii", errors="strict").splitlines():
            values = line.split()
            if len(values) > max(indices):
                rows.append([float(values[index]) for index in indices])
        xyz = np.asarray(rows, dtype=np.float32)
    elif mode == "binary":
        dtype_fields = []
        for name, size, point_type, count in zip(fields, sizes, types, counts):
            code = _PCD_DTYPES.get((point_type.upper(), size))
            if code is None:
                raise ValueError(f"unsupported PCD field type: {point_type}{size}")
            dtype_fields.append((name, code, (count,)) if count > 1 else (name, code))
        dtype = np.dtype(dtype_fields)
        if len(payload) < points * dtype.itemsize:
            raise ValueError("PCD binary payload is shorter than declared")
        cloud = np.frombuffer(payload, dtype=dtype, count=points)
        xyz = np.column_stack((cloud["x"], cloud["y"], cloud["z"])).astype(np.float32, copy=False)
    elif mode == "binary_compressed":
        if len(payload) < 8:
            raise ValueError("compressed PCD payload is incomplete")
        compressed_size, uncompressed_size = struct.unpack_from("<II", payload)
        compressed = payload[8:8 + compressed_size]
        if len(compressed) != compressed_size:
            raise ValueError("compressed PCD payload is truncated")
        raw = _lzf_decompress(compressed, uncompressed_size)
        cursor = 0
        arrays: dict[str, np.ndarray] = {}
        for name, size, point_type, count in zip(fields, sizes, types, counts):
            code = _PCD_DTYPES.get((point_type.upper(), size))
            if code is None:
                raise ValueError(f"unsupported PCD field type: {point_type}{size}")
            byte_count = points * size * count
            block = raw[cursor:cursor + byte_count]
            if len(block) != byte_count:
                raise ValueError("uncompressed PCD payload is truncated")
            if name in {"x", "y", "z"} and count == 1:
                arrays[name] = np.frombuffer(block, dtype=np.dtype(code), count=points)
            cursor += byte_count
        xyz = np.column_stack((arrays["x"], arrays["y"], arrays["z"])).astype(np.float32, copy=False)
    else:
        raise ValueError(f"unsupported PCD DATA mode: {mode}")
    xyz = xyz[np.isfinite(xyz).all(axis=1)]
    if len(xyz) > max_points:
        xyz = xyz[np.linspace(0, len(xyz) - 1, max_points, dtype=np.int64)]
    return np.ascontiguousarray(xyz, dtype=np.float32)


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "MapViewer/1.0"

    @property
    def viewer_server(self) -> "MapViewerServer":
        return self.server  # type: ignore[return-value]

    def _send_bytes(self, content: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, value: object, status: int = 200) -> None:
        self._send_bytes(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def _read_json(self) -> object:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 8 * 1024 * 1024:
            raise ValueError("request body is too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        request_path = unquote(urlparse(self.path).path)
        if request_path == "/api/scene":
            scene = self.viewer_server.get_scene()
            self._send_bytes(json.dumps(scene.metadata(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if request_path == "/api/pointcloud.bin":
            scene = self.viewer_server.get_scene()
            self._send_bytes(scene.pointcloud.astype("<f4", copy=False).tobytes(), "application/octet-stream")
            return
        if request_path.startswith("/assets/"):
            relative = Path(request_path.removeprefix("/assets/"))
            target = (self.viewer_server.map_dir / relative).resolve()
            if target.is_file() and self.viewer_server.map_dir.resolve() in target.parents:
                self._send_bytes(target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                return
            static = (self.viewer_server.static_dir / relative).resolve()
            if static.is_file() and self.viewer_server.static_dir.resolve() in static.parents:
                self._send_bytes(static.read_bytes(), mimetypes.guess_type(static.name)[0] or "application/octet-stream")
                return
        if request_path in {"/", "/index.html"}:
            target = self.viewer_server.static_dir / "index.html"
            self._send_bytes(target.read_bytes(), "text/html; charset=utf-8")
            return
        self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    def do_POST(self) -> None:  # noqa: N802
        request_path = unquote(urlparse(self.path).path)
        if request_path != "/api/transitions":
            self._send_json({"error": "Not found"}, 404)
            return
        try:
            payload = self._read_json()
            if not isinstance(payload, dict) or not isinstance(payload.get("transitions"), list):
                raise ValueError("transitions must be a JSON array")
            save_transition_rows(self.viewer_server.map_dir, payload["transitions"])
            self.viewer_server.scene = None
            self._send_json({"ok": True, "scene": self.viewer_server.get_scene().metadata()})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[map-viewer] {fmt % args}", file=sys.stderr)


class MapViewerServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], map_dir: Path, max_points: int) -> None:
        super().__init__(address, ViewerHandler)
        self.map_dir = map_dir
        self.max_points = max_points
        self.static_dir = Path(__file__).resolve().parent / "viewer"
        self.scene: SceneData | None = None

    def get_scene(self) -> SceneData:
        if self.scene is None:
            self.scene = load_map_project(self.map_dir, self.max_points)
        return self.scene


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a GridMapper project in a local 3D viewer")
    default_map_dir = Path(__file__).resolve().parent / "maps"
    parser.add_argument("--map-dir", type=Path, default=default_map_dir)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-points", type=int, default=150_000)
    args = parser.parse_args()
    server = MapViewerServer((args.host, args.port), args.map_dir.expanduser().resolve(), args.max_points)
    print(f"Map viewer: http://{args.host}:{args.port}")
    print(f"Map directory: {server.map_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
