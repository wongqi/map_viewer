import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from map_viewer import load_map_project, load_pcd_xyz


def _write_project(root: Path) -> None:
    (root / "map_000.png").write_bytes(b"not used")
    Image.new("RGBA", (10, 20), (255, 255, 255, 255)).save(root / "map_000.png")
    Image.new("RGBA", (20, 10), (255, 255, 255, 255)).save(root / "map_0222.png")
    (root / "map_000.yaml").write_text("image: map_000.png\nresolution: 0.5\norigin: [-1, -2, 0]\n")
    (root / "map_0222.yaml").write_text("image: map_0222.png\nresolution: 0.5\norigin: [-2, -3, 0]\n")
    (root / "map_relations.csv").write_text("from_map,to_map,dx,dy\nROOT,map_000,0,0\nmap_000,map_0222,4,5\n")
    (root / "transition_points.csv").write_text("transition_id,from_map,to_map,world_x,world_y,world_z,world_yaw_rad,bidirectional,type\ntp_1,map_000,map_0222,4,5,2,0,true,stairs\n")


class ViewerTests(unittest.TestCase):
    def test_project_coordinates_and_estimated_heights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_project(root)
            scene = load_map_project(root)
            records = {record.map_id: record for record in scene.maps}
            self.assertEqual(records["map_0222"].root_offset, [4.0, 5.0])
            self.assertEqual(records["map_0222"].world_origin, [2.0, 2.0, 2.0])
            self.assertEqual(records["map_000"].size, [5.0, 10.0])
            self.assertEqual(records["map_000"].z_source, "estimated_from_transitions")
            self.assertEqual(len(scene.transitions), 1)
            self.assertTrue(scene.transitions[0].in_range)

    def test_ascii_pcd(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloud.pcd"
            path.write_text("FIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nPOINTS 2\nDATA ascii\n1 2 3\n4 5 6\n")
            np.testing.assert_allclose(load_pcd_xyz(path), [[1, 2, 3], [4, 5, 6]])

    def test_binary_compressed_pcd_literal_lzf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloud.pcd"
            # PCL compressed layout stores one field block after another.
            raw = struct.pack("<2f", 1, 4) + struct.pack("<2f", 2, 5) + struct.pack("<2f", 3, 6)
            compressed = bytes([len(raw) - 1]) + raw
            header = "FIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nPOINTS 2\nDATA binary_compressed\n"
            path.write_bytes(header.encode() + struct.pack("<II", len(compressed), len(raw)) + compressed)
            np.testing.assert_allclose(load_pcd_xyz(path), [[1, 2, 3], [4, 5, 6]])


if __name__ == "__main__":
    unittest.main()
