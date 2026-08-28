# 三维多地图查看器

独立的离线地图查看工具，用于加载 GridMapper 导出的地图项目，不启动 ROS，也不修改建图或导航文件。

## 启动

在目录 `/map_viewer` 下执行：

```bash
python3 -m map_viewer \
  --map-dir /home/wangqi/colcon_ws/unitree_go1_ws/src/map_viewer/maps \
  --host 127.0.0.1 \
  --port 8765
```

然后打开 `http://127.0.0.1:8765`。

默认地图目录为 `/map_viewer/maps`，也可以省略 `--map-dir`。查看器读取 `map_*.yaml`、`map_*.png`、`map_relations.csv`、`transition_points.csv` 和 `PGO.pcd`，支持 ASCII、binary、binary_compressed 三种 PCD DATA 格式。

## 依赖

```bash
python3 -m pip install numpy Pillow
```

`context/inspection_context.yaml` 存在时可选安装 `PyYAML` 读取精确的 `z_p50`；没有该文件时，地图高度会从传送点高度估计，并在页面中标注来源。
