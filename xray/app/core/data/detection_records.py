"""
检测记录数据库管理模块
使用SQLite存储和管理检测结果
"""
import sqlite3
import os
import json
from datetime import datetime
from pathlib import Path


def make_json_serializable(obj):
    """
    递归转换对象为JSON可序列化格式
    
    处理以下类型：
    - numpy.ndarray → list
    - numpy.int/float → int/float  
    - torch.Tensor → list
    - 其他不可序列化对象 → str
    
    Args:
        obj: 任意Python对象
        
    Returns:
        JSON可序列化的对象
    """
    import numpy as np
    
    if obj is None:
        return None
        
    # 基本类型直接返回
    if isinstance(obj, (str, int, float, bool)):
        return obj
        
    # numpy数组 → 列表
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
        
    # numpy标量 → Python标量
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
        
    # PyTorch张量 - Win7上torch可能半初始化(Tensor属性不存在)
    try:
        import torch
        if hasattr(torch, 'Tensor') and isinstance(obj, torch.Tensor):
            if obj.numel() == 1:
                return obj.item()
            return obj.cpu().numpy().tolist()
    except (ImportError, OSError, AttributeError):
        pass
        
    # 字典 → 递归处理每个值
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
        
    # 列表/元组 → 递归处理每个元素
    if isinstance(obj, (list, tuple)):
        return [make_json_serializable(item) for item in obj]
        
    # 集合 → 转列表
    if isinstance(obj, set):
        return [make_json_serializable(item) for item in obj]
        
    # 其他类型 → 尝试转字符串
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


class DetectionRecordDB:
    """检测记录数据库管理类"""
    
    def __init__(self, db_path: str = None):
        """
        初始化数据库连接
        
        Args:
            db_path: 数据库文件路径，默认为 data/detection_records.db
        """
        if db_path is None:
            # 默认路径：项目根目录下的data文件夹
            base_dir = Path(__file__).parent.parent.parent
            data_dir = base_dir / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "detection_records.db")
        
        self.db_path = db_path
        self.conn = None
        self._connect()
        self._create_tables()
    
    def _connect(self):
        """建立数据库连接"""
        try:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row  # 返回字典式行对象
        except Exception as e:
            print(f"数据库连接失败: {e}")
            raise
    
    def _create_tables(self):
        """创建数据表（如果不存在）"""
        cursor = self.conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS detection_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detection_time TEXT NOT NULL,
                patient_name TEXT,
                patient_id TEXT,
                access_no TEXT,
                study_date TEXT,
                image_path TEXT,
                detection_type TEXT,  -- 'chest_xray' 或 'bone_age' 或 'fracture'
                results_json TEXT,    -- 检测结果（JSON格式）
                summary TEXT,         -- 诊断摘要
                model_version TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 创建索引加速查询
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_detection_time ON detection_records(detection_time)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_patient_name ON detection_records(patient_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_detection_type ON detection_records(detection_type)')
        
        self.conn.commit()
    
    def add_record(self, record_data: dict) -> int:
        """
        添加一条检测记录
        
        Args:
            record_data: 记录数据字典，包含：
                - patient_name: 患者姓名
                - patient_id: 患者ID
                - access_no: 检查号
                - study_date: 检查日期
                - image_path: 图像路径
                - detection_type: 检测类型
                - results_json: 结果JSON
                - summary: 诊断摘要
                - model_version: 模型版本
        
        Returns:
            新记录的ID
        """
        cursor = self.conn.cursor()
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 转换results为JSON可序列化格式
        results = record_data.get('results', {})
        serializable_results = make_json_serializable(results)
        
        cursor.execute('''
            INSERT INTO detection_records 
            (detection_time, patient_name, patient_id, access_no, study_date, 
             image_path, detection_type, results_json, summary, model_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            now,
            record_data.get('patient_name', ''),
            record_data.get('patient_id', ''),
            record_data.get('access_no', ''),
            record_data.get('study_date', ''),
            record_data.get('image_path', ''),
            record_data.get('detection_type', 'chest_xray'),
            json.dumps(serializable_results, ensure_ascii=False),
            record_data.get('summary', ''),
            record_data.get('model_version', '')
        ))
        
        self.conn.commit()
        return cursor.lastrowid
    
    def get_records(self, page: int = 1, per_page: int = 10, 
                    search_text: str = None, detection_type: str = None) -> dict:
        """
        获取检测记录（分页）
        
        Args:
            page: 页码（从1开始）
            per_page: 每页记录数
            search_text: 搜索关键词（患者姓名/检查号）
            detection_type: 检测类型过滤
        
        Returns:
            包含记录列表和分页信息的字典
        """
        cursor = self.conn.cursor()
        
        # 构建查询条件
        conditions = []
        params = []
        
        if search_text:
            conditions.append("(patient_name LIKE ? OR access_no LIKE ?)")
            params.extend([f'%{search_text}%', f'%{search_text}%'])
        
        if detection_type and detection_type != 'all':
            conditions.append("detection_type = ?")
            params.append(detection_type)
        
        where_clause = ""
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)
        
        # 查询总数
        count_sql = f"SELECT COUNT(*) FROM detection_records{where_clause}"
        cursor.execute(count_sql, params)
        total = cursor.fetchone()[0]
        
        # 计算分页
        offset = (page - 1) * per_page
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        
        # 查询记录（按时间倒序）
        query_sql = f'''
            SELECT * FROM detection_records{where_clause}
            ORDER BY detection_time DESC
            LIMIT ? OFFSET ?
        '''
        params.extend([per_page, offset])
        cursor.execute(query_sql, params)
        
        records = [dict(row) for row in cursor.fetchall()]
        
        return {
            'records': records,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages
        }
    
    def get_record_by_id(self, record_id: int) -> dict:
        """根据ID获取单条记录"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM detection_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def delete_record(self, record_id: int) -> bool:
        """删除单条记录"""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM detection_records WHERE id = ?", (record_id,))
        self.conn.commit()
        return cursor.rowcount > 0
    
    def delete_records(self, record_ids: list) -> int:
        """批量删除记录"""
        cursor = self.conn.cursor()
        placeholders = ','.join(['?'] * len(record_ids))
        cursor.execute(f"DELETE FROM detection_records WHERE id IN ({placeholders})", record_ids)
        self.conn.commit()
        return cursor.rowcount
    
    def clear_all_records(self) -> int:
        """清空所有记录"""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM detection_records")
        self.conn.commit()
        return cursor.rowcount
    
    def export_to_csv(self, output_path: str = None, 
                      search_text: str = None, 
                      detection_type: str = None) -> str:
        """
        导出记录到CSV文件
        
        Returns:
            导出文件的完整路径
        """
        import csv
        
        if output_path is None:
            base_dir = Path(self.db_path).parent
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(base_dir / f"detection_records_{timestamp}.csv")
        
        # 获取所有符合条件的记录
        result = self.get_records(page=1, per_page=100000, 
                                 search_text=search_text, 
                                 detection_type=detection_type)
        records = result['records']
        
        with open(output_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            
            # 写入表头
            writer.writerow([
                '序号', '检测时间', '患者姓名', '检查号', '检查日期',
                '图像路径', '检测类型', '诊断摘要', '模型版本'
            ])
            
            # 写入数据
            for i, record in enumerate(records, 1):
                writer.writerow([
                    i,
                    record['detection_time'],
                    record['patient_name'],
                    record['access_no'],
                    record['study_date'],
                    record['image_path'],
                    record['detection_type'],
                    record['summary'],
                    record['model_version']
                ])
        
        return output_path
    
    def export_to_excel(self, output_path: str = None,
                       search_text: str = None,
                       detection_type: str = None) -> str:
        """
        导出记录到Excel文件（需要openpyxl或xlsxwriter库）
        
        Returns:
            导出文件的完整路径
        """
        try:
            import openpyxl
            from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        except ImportError:
            # 如果没有openpyxl，回退到CSV
            return self.export_to_csv(output_path, search_text, detection_type)
        
        if output_path is None:
            base_dir = Path(self.db_path).parent
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(base_dir / f"detection_records_{timestamp}.xlsx")
        
        # 获取所有记录
        result = self.get_records(page=1, per_page=100000,
                                 search_text=search_text,
                                 detection_type=detection_type)
        records = result['records']
        
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "检测记录"
        
        # 定义样式
        header_font = Font(bold=True, size=11)
        header_fill = PatternFill(start_color="0078D4", end_color="0078D4", fill_type="solid")
        header_font_white = Font(bold=True, size=11, color="FFFFFF")
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        # 写入表头
        headers = ['序号', '检测时间', '患者姓名', '检查号', '检查日期',
                  '图像路径', '检测类型', '诊断摘要', '模型版本']
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font_white
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.border = thin_border
        
        # 写入数据
        for row_idx, record in enumerate(records, 2):
            data = [
                row_idx - 1,
                record['detection_time'],
                record['patient_name'],
                record['access_no'],
                record['study_date'],
                record['image_path'],
                record['detection_type'],
                record['summary'],
                record['model_version']
            ]
            
            for col_idx, value in enumerate(data, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.border = thin_border
                cell.alignment = Alignment(vertical='center')
        
        # 自动调整列宽
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws.column_dimensions[column_letter].width = adjusted_width
        
        wb.save(output_path)
        return output_path
    
    def get_statistics(self) -> dict:
        """获取统计信息"""
        cursor = self.conn.cursor()
        
        # 总记录数
        cursor.execute("SELECT COUNT(*) FROM detection_records")
        total = cursor.fetchone()[0]
        
        # 今日检测数
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute("SELECT COUNT(*) FROM detection_records WHERE detection_time LIKE ?", (f'{today}%',))
        today_count = cursor.fetchone()[0]
        
        # 按类型统计
        cursor.execute("""
            SELECT detection_type, COUNT(*) as count 
            FROM detection_records 
            GROUP BY detection_type
        """)
        type_stats = {row[0]: row[1] for row in cursor.fetchall()}
        
        return {
            'total': total,
            'today_count': today_count,
            'by_type': type_stats
        }
    
    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
    
    def __del__(self):
        """析构函数，自动关闭连接"""
        self.close()


# 全局数据库实例
_db_instance = None

def get_db() -> DetectionRecordDB:
    """获取数据库单例实例"""
    global _db_instance
    if _db_instance is None:
        _db_instance = DetectionRecordDB()
    return _db_instance
