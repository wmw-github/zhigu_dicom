"""
坐标转换和同步工具模块
用于解决MPR视图和3D视图之间的方向一致性以及光标同步问题
"""

import math

import numpy as np
from typing import Tuple, Optional


class CoordinateConverter:
    """坐标转换器，处理不同视图间的坐标转换"""
    
    def __init__(self):
        self.origin = None
        self.spacing = None
        self.extent = None
        
    def set_image_properties(self, origin: Tuple[float, float, float], 
                           spacing: Tuple[float, float, float],
                           extent: Tuple[int, int, int, int, int, int]):
        """设置图像的基本属性"""
        self.origin = np.array(origin, dtype=np.float64)
        self.spacing = np.array(spacing, dtype=np.float64)
        self.extent = extent
    
    def ijk_to_world(self, i: int, j: int, k: int) -> Tuple[float, float, float]:
        """将图像坐标(IJK)转换为世界坐标"""
        if self.origin is None or self.spacing is None:
            return (0.0, 0.0, 0.0)
        
        world_x = self.origin[0] + i * self.spacing[0]
        world_y = self.origin[1] + j * self.spacing[1]
        world_z = self.origin[2] + k * self.spacing[2]
        return (world_x, world_y, world_z)
    
    def world_to_ijk(self, world_x: float, world_y: float, world_z: float) -> Tuple[int, int, int]:
        """将世界坐标转换为图像坐标(IJK)"""
        if self.origin is None or self.spacing is None:
            return (0, 0, 0)

        # 间距非正或非有限值（例如清空数据后残留 NaN）时无法换算，返回原点
        for s in self.spacing:
            if not math.isfinite(float(s)) or float(s) <= 0:
                return (0, 0, 0)
        for v in (world_x, world_y, world_z):
            if not math.isfinite(float(v)):
                return (0, 0, 0)

        i = int(round((world_x - self.origin[0]) / self.spacing[0]))
        j = int(round((world_y - self.origin[1]) / self.spacing[1]))
        k = int(round((world_z - self.origin[2]) / self.spacing[2]))
        
        # 确保在有效范围内
        if self.extent:
            i = max(self.extent[0], min(self.extent[1], i))
            j = max(self.extent[2], min(self.extent[3], j))
            k = max(self.extent[4], min(self.extent[5], k))
        
        return (i, j, k)
    
    def get_slice_index_for_view(self, world_pos: Tuple[float, float, float], 
                               view_orientation: str) -> int:
        """根据视图方向获取对应的切片索引"""
        if self.origin is None or self.spacing is None:
            return 0
            
        world_x, world_y, world_z = world_pos
        
        if view_orientation == "axial":
            # 轴向视图显示XY平面，切片沿Z轴
            return int(round((world_z - self.origin[2]) / self.spacing[2]))
        elif view_orientation == "sagittal":
            # 矢状视图显示YZ平面，切片沿X轴
            return int(round((world_x - self.origin[0]) / self.spacing[0]))
        elif view_orientation == "coronal":
            # 冠状视图显示XZ平面，切片沿Y轴
            return int(round((world_y - self.origin[1]) / self.spacing[1]))
        else:
            return 0


class ViewDirectionManager:
    """视图方向管理器，确保所有视图方向一致性"""
    
    @staticmethod
    def get_standard_camera_settings(view_orientation: str) -> dict:
        """获取标准的相机设置，确保视图方向一致性"""
        settings = {
            "axial": {
                "view_up": (0, -1, 0),      # 上方为负Y轴
                "position_offset": (0, 0, -1)  # 相机在Z轴负方向
            },
            "sagittal": {
                "view_up": (0, -1, 0),      # 上方为负Y轴（与轴向一致）
                "position_offset": (-1, 0, 0)  # 相机在X轴负方向
            },
            "coronal": {
                "view_up": (0, -1, 0),      # 与矢状位和轴向保持一致
                "position_offset": (0, -1, 0)  # 相机在Y轴负方向
            }
        }
        return settings.get(view_orientation, settings["axial"])
    
    @staticmethod
    def adjust_camera_for_consistency(camera, view_orientation: str, 
                                    center_point: Tuple[float, float, float],
                                    max_dimension: float):
        """调整相机设置以确保方向一致性"""
        settings = ViewDirectionManager.get_standard_camera_settings(view_orientation)
        
        # 设置视图向上方向
        camera.SetViewUp(*settings["view_up"])
        
        # 计算相机位置
        offset = np.array(settings["position_offset"]) * max_dimension * 2.0
        camera_pos = np.array(center_point) + offset
        
        camera.SetPosition(*camera_pos)
        camera.SetFocalPoint(*center_point)


class CursorSynchronizer:
    """光标同步管理器"""
    
    def __init__(self):
        self.coordinate_converter = CoordinateConverter()
        self.callbacks = []
        self.current_world_pos = None
        
    def set_image_properties(self, origin, spacing, extent):
        """设置图像属性"""
        self.coordinate_converter.set_image_properties(origin, spacing, extent)
        
    def register_callback(self, callback):
        """注册光标位置变化回调函数"""
        if callback not in self.callbacks:
            self.callbacks.append(callback)
            
    def unregister_callback(self, callback):
        """注销回调函数"""
        if callback in self.callbacks:
            self.callbacks.remove(callback)
            
    def update_cursor_position(self, i: int, j: int, k: int, source_view: str = None):
        """更新光标位置"""
        world_pos = self.coordinate_converter.ijk_to_world(i, j, k)
        self.current_world_pos = world_pos
        self._notify_callbacks(world_pos, source_view)
        
    def set_world_position(self, world_pos: Tuple[float, float, float], source_view: str = None):
        """直接设置世界坐标位置"""
        self.current_world_pos = world_pos
        self._notify_callbacks(world_pos, source_view)
        
    def get_current_world_position(self) -> Optional[Tuple[float, float, float]]:
        """获取当前世界坐标位置"""
        return self.current_world_pos
        
    def _notify_callbacks(self, world_pos: Tuple[float, float, float], source_view: str = None):
        """通知所有注册的回调函数"""
        for callback in self.callbacks:
            try:
                callback(world_pos, source_view)
            except Exception as e:
                print(f"回调函数执行错误: {e}")
                
    def get_view_positions(self, world_pos: Tuple[float, float, float]) -> dict:
        """获取指定世界坐标在各视图中的位置信息"""
        if self.coordinate_converter.origin is None:
            return {}
            
        i, j, k = self.coordinate_converter.world_to_ijk(*world_pos)
        
        # 获取图像范围以进行正确的翻转计算
        extent = self.coordinate_converter.extent
        max_i = 511  # 默认值
        if extent:
            max_i = extent[1]
            
        positions = {
            "axial": {
                "slice_index": k,
                "cursor_pos": (i, j)
            },
            "sagittal": {
                "slice_index": i,
                "cursor_pos": (k, j)
            },
            "coronal": {
                "slice_index": j,
                "cursor_pos": (i, k)  # 移除这里的翻转逻辑，统一由SliceView处理
            }
        }
        return positions