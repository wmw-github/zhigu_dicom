#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
危急值检测模块
用于检测影像报告中的危急值
"""

import os
import json
import re
from typing import List, Dict, Optional


class CriticalValueDetector:
    """危急值检测器"""

    def __init__(self, config_path: str = None):
        """
        初始化危急值检测器

        Args:
            config_path: 配置文件路径
        """
        if config_path is None:
            # 规则 JSON 统一放在 reportqc/config/（与 qc/ 平级），
            # 与 checker_base._get_config_dir() 保持同一口径。
            _here = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(
                os.path.dirname(_here), 'config', 'critical_values_config.json')

        self.config_path = config_path
        self.critical_values = []
        self._load_config()

    def _load_config(self):
        """加载危急值配置"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.critical_values = config.get('critical_values', [])
                print(f"已加载 {len(self.critical_values)} 个危急值标准")
            else:
                print(f"配置文件不存在: {self.config_path}")
                # 使用默认配置
                self._load_default_config()
        except Exception as e:
            print(f"加载危急值配置失败: {e}")
            self._load_default_config()

    def _load_default_config(self):
        """加载默认危急值配置"""
        self.critical_values = [
            {
                'id': 'CV001',
                'category': '中枢神经系统',
                'keywords': ['脑干出血', '脑干梗死', '脑疝', '蛛网膜下腔出血', '脑出血', '脑梗死', '大面积脑梗死'],
                'description': '中枢神经系统危急值',
                'severity': 'critical'
            },
            {
                'id': 'CV002',
                'category': '创伤急症',
                'keywords': ['肝破裂', '脾破裂', '胰破裂', '肾破裂', '腹腔脏器破裂', '大出血', '内脏破裂'],
                'description': '外伤致腹腔脏器破裂',
                'severity': 'critical'
            }
        ]

    def detect_critical_values(self, text: str, patient_id: str = None, patient_name: str = None) -> List[Dict]:
        """
        检测文本中的危急值

        Args:
            text: 待检测的文本（通常是诊断结论部分）
            patient_id: 患者ID
            patient_name: 患者姓名

        Returns:
            检测到的危急值列表
        """
        critical_findings = []

        # 提取诊断结论部分（如果有）
        conclusion_text = self._extract_conclusion(text)
        if not conclusion_text:
            conclusion_text = text

        # 遍历所有危急值标准
        for cv in self.critical_values:
            cv_id = cv.get('id', '')
            category = cv.get('category', '')
            keywords = cv.get('keywords', [])
            description = cv.get('description', '')
            severity = cv.get('severity', 'critical')

            # 检查关键词
            for keyword in keywords:
                if keyword in conclusion_text:
                    # 提取上下文
                    context = self._extract_context(conclusion_text, keyword)

                    critical_findings.append({
                        'cv_id': cv_id,
                        'category': category,
                        'keyword': keyword,
                        'description': description,
                        'severity': severity,
                        'context': context,
                        'patient_id': patient_id,
                        'patient_name': patient_name,
                        'full_text': conclusion_text[:200]
                    })

                    # 找到一个关键词就跳出，避免重复
                    break

        return critical_findings

    def _extract_conclusion(self, text: str) -> str:
        """
        提取诊断结论部分

        Args:
            text: 完整报告文本

        Returns:
            诊断结论部分
        """
        # 常见的结论标题
        conclusion_patterns = [
            r'(?:诊断结论|影像结论|诊断意见|结论|影像提示|提示)[：:\s]*(.*?)(?:$)',
            r'(?:诊断|影像诊断)[：:\s]*(.*?)(?:$)',
        ]

        for pattern in conclusion_patterns:
            match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()

        # 如果没有找到明确的结论部分，返回全文
        return text

    def _extract_context(self, text: str, keyword: str, context_length: int = 50) -> str:
        """
        提取关键词上下文

        Args:
            text: 文本
            keyword: 关键词
            context_length: 上下文长度

        Returns:
            包含关键词的上下文
        """
        idx = text.find(keyword)
        if idx == -1:
            return keyword

        start = max(0, idx - context_length)
        end = min(len(text), idx + len(keyword) + context_length)

        context = text[start:end]
        if start > 0:
            context = '...' + context
        if end < len(text):
            context = context + '...'

        return context

    def is_critical_patient(self, patient_id: str) -> bool:
        """
        检查患者是否为危急值患者

        Args:
            patient_id: 患者ID

        Returns:
            是否为危急值患者
        """
        # 这个方法需要查询数据库中的危急值记录
        # 暂时返回False，后续在数据库管理器中实现
        return False

    def get_critical_value_categories(self) -> List[str]:
        """
        获取所有危急值类别

        Returns:
            危急值类别列表
        """
        return [cv.get('category', '') for cv in self.critical_values if cv.get('category')]

    def add_custom_critical_value(self, category: str, keyword: str, description: str):
        """
        添加自定义危急值

        Args:
            category: 类别
            keyword: 关键词
            description: 描述
        """
        # 查找或创建该类别
        for cv in self.critical_values:
            if cv.get('category') == category:
                if keyword not in cv.get('keywords', []):
                    cv['keywords'].append(keyword)
                return

        # 创建新类别
        self.critical_values.append({
            'id': f'CV{len(self.critical_values) + 1:03d}',
            'category': category,
            'keywords': [keyword],
            'description': description,
            'severity': 'critical'
        })

        # 保存到配置文件
        self._save_config()

    def _save_config(self):
        """保存配置到文件"""
        try:
            config = {
                'critical_values': self.critical_values,
                'version': '1.0',
                'update_date': str(__import__('datetime').datetime.now().date()),
                'source': '用户自定义'
            }

            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)

            print(f"危急值配置已保存: {self.config_path}")
        except Exception as e:
            print(f"保存危急值配置失败: {e}")


# 测试代码
if __name__ == '__main__':
    detector = CriticalValueDetector()

    # 测试文本
    test_text = """
    影像所见：
    双肺纹理清晰，未见明显异常密度影。
    心影大小形态正常。

    诊断结论：
    1. 脑干出血
    2. 蛛网膜下腔出血（累及基底池、侧裂池、鞍上池）
    """

    critical_findings = detector.detect_critical_values(test_text, 'P001', '测试患者')

    print("=" * 80)
    print(f"检测到 {len(critical_findings)} 个危急值:")
    print("=" * 80)

    for finding in critical_findings:
        print(f"\n类别: {finding['category']}")
        print(f"关键词: {finding['keyword']}")
        print(f"描述: {finding['description']}")
        print(f"上下文: {finding['context']}")
        print(f"严重程度: {finding['severity']}")

    print("\n所有危急值类别:")
    print(detector.get_critical_value_categories())