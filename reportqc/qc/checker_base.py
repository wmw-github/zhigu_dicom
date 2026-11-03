#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能文本检查器基础类
"""

import re
import json
import os
import sys
from typing import List, Dict
from difflib import SequenceMatcher
from collections import Counter

try:
    from .database_manager import DatabaseManager
    DATABASE_AVAILABLE = True
except ImportError:
    DATABASE_AVAILABLE = False


class BaseTextChecker:
    """智能文本检查器基础类"""

    def __init__(self):
        """初始化智能检查器"""
        # 确定文件存储路径
        self.config_dir = self._get_config_dir()

        # 加载所有配置文件
        self._load_config_files()

        # 初始化学习模式
        self.learned_patterns = self._load_learned_patterns()

        # 初始化数据库管理器（如果可用）
        if DATABASE_AVAILABLE:
            try:
                self.db = DatabaseManager()
                self._load_false_positives_from_db()
            except Exception as e:
                print(f"数据库初始化失败，将使用传统方式: {e}")
                self.db = None
        else:
            self.db = None

    def _get_config_dir(self):
        """获取配置目录路径。

        规则 JSON 统一放在 ``reportqc/config/``（与 ``qc/`` 平级），
        便于随绿色包分发与维护；不再按本文件位置找 ``qc/config/``。
        """
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            # 本文件在 reportqc/qc/ 下，配置在其上一级的 reportqc/config/
            base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        config_dir = os.path.join(base_path, "config")
        if not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)

        return config_dir

    def _load_config_files(self):
        """加载所有配置文件"""
        # 加载配置文件路径
        self.custom_typos_file = os.path.join(self.config_dir, "custom_typos.json")
        self.medical_whitelist_file = os.path.join(self.config_dir, "medical_whitelist.json")
        self.exception_rules_file = os.path.join(self.config_dir, "exception_rules.json")
        self.learned_patterns_file = os.path.join(self.config_dir, "learned_patterns.json")
        self.medical_patterns_file = os.path.join(self.config_dir, "medical_patterns.json")
        self.contradiction_patterns_file = os.path.join(self.config_dir, "contradiction_patterns.json")
        self.medical_continuous_file = os.path.join(self.config_dir, "medical_continuous_chars.json")
        self.additional_medical_file = os.path.join(self.config_dir, "additional_medical_words.json")

        # 加载预设错别字（常见医学错别字）
        self.preset_typos = {
            # ==================== 解剖部位错别字 ====================
            # "膈"与"隔"的混用（医学中最常见的错误）
            '纵膈': {'correction': '纵隔', 'severity': 'high', 'category': '解剖部位'},
            '横膈': {'correction': '横隔', 'severity': 'high', 'category': '解剖部位'},  # 注：横膈是正确的写法
            '隔肌': {'correction': '膈肌', 'severity': 'high', 'category': '解剖部位'},
            '隔疝': {'correction': '膈疝', 'severity': 'high', 'category': '解剖部位'},
            '隔下': {'correction': '膈下', 'severity': 'high', 'category': '解剖部位'},
            '隔面': {'correction': '膈面', 'severity': 'high', 'category': '解剖部位'},
            '肋隔角': {'correction': '肋膈角', 'severity': 'high', 'category': '解剖部位'},
            '盆隔': {'correction': '盆膈', 'severity': 'high', 'category': '解剖部位'},
            '尿生殖隔': {'correction': '尿生殖膈', 'severity': 'high', 'category': '解剖部位'},
            
            # 骨骼相关
            '骨胳': {'correction': '骨骼', 'severity': 'high', 'category': '解剖部位'},
            '干骺段': {'correction': '干骺端', 'severity': 'high', 'category': '解剖部位'},
            '骨骺端': {'correction': '干骺端', 'severity': 'high', 'category': '解剖部位'},
            '岗上肌': {'correction': '冈上肌', 'severity': 'high', 'category': '解剖部位'},
            '岗下肌': {'correction': '冈下肌', 'severity': 'high', 'category': '解剖部位'},
            
            # ==================== 疾病与体征错别字 ====================
            '肺大泡': {'correction': '肺大疱', 'severity': 'high', 'category': '疾病'},
            '肺大胞': {'correction': '肺大疱', 'severity': 'high', 'category': '疾病'},
            '痉孪': {'correction': '痉挛', 'severity': 'high', 'category': '疾病'},
            '麻庳': {'correction': '麻痹', 'severity': 'high', 'category': '疾病'},
            '梗阻': {'correction': '梗阻', 'severity': 'low', 'category': '疾病'},  # 正确写法，用于排除
            
            # ==================== 术语规范错别字 ====================
            '适应症': {'correction': '适应证', 'severity': 'medium', 'category': '术语规范'},
            '禁忌症': {'correction': '禁忌证', 'severity': 'medium', 'category': '术语规范'},
            '综合症': {'correction': '综合征', 'severity': 'medium', 'category': '术语规范'},
            '症候群': {'correction': '征候群', 'severity': 'medium', 'category': '术语规范'},
            
            # ==================== 肌腱相关错别字 ====================
            '肌建': {'correction': '肌腱', 'severity': 'high', 'category': '解剖部位'},
            '跟建': {'correction': '跟腱', 'severity': 'high', 'category': '解剖部位'},
            '髌建': {'correction': '髌腱', 'severity': 'high', 'category': '解剖部位'},
            '踺': {'correction': '腱', 'severity': 'high', 'category': '解剖部位'},
            
            # ==================== 常见医学词汇错别字 ====================
            '辐谢': {'correction': '辐射', 'severity': 'high', 'category': '医学术语'},
            '幅射': {'correction': '辐射', 'severity': 'high', 'category': '医学术语'},
            '痉孪': {'correction': '痉挛', 'severity': 'high', 'category': '医学术语'},
            '挛缩': {'correction': '挛缩', 'severity': 'low', 'category': '医学术语'},  # 正确写法
            '憩室': {'correction': '憩室', 'severity': 'low', 'category': '医学术语'},  # 正确写法
            '憩室病': {'correction': '憩室病', 'severity': 'low', 'category': '医学术语'},
            '憩室炎': {'correction': '憩室炎', 'severity': 'low', 'category': '医学术语'},
            
            # ==================== 影像描述常见错别字 ====================
            '班片': {'correction': '斑片', 'severity': 'high', 'category': '影像描述'},
            '班点': {'correction': '斑点', 'severity': 'high', 'category': '影像描述'},
            '班纹': {'correction': '斑纹', 'severity': 'high', 'category': '影像描述'},
            '结节壮': {'correction': '结节状', 'severity': 'high', 'category': '影像描述'},
            '团块壮': {'correction': '团块状', 'severity': 'high', 'category': '影像描述'},
            '条絮状': {'correction': '条片状', 'severity': 'high', 'category': '影像描述'},
            
            # ==================== 常用词错别字 ====================
            '瘢痕': {'correction': '瘢痕', 'severity': 'low', 'category': '常用词'},  # 正确写法
            '疤痕': {'correction': '瘢痕', 'severity': 'medium', 'category': '常用词'},
            '黏连': {'correction': '粘连', 'severity': 'medium', 'category': '常用词'},
            '粘液': {'correction': '黏液', 'severity': 'medium', 'category': '常用词'},
            '粘膜': {'correction': '黏膜', 'severity': 'medium', 'category': '常用词'},
            '罗音': {'correction': '啰音', 'severity': 'medium', 'category': '常用词'},
            '兰尾': {'correction': '阑尾', 'severity': 'high', 'category': '常用词'},
            '兰色': {'correction': '蓝色', 'severity': 'high', 'category': '常用词'},
            
            # ==================== 神经系统相关 ====================
            '脑实质': {'correction': '脑实质', 'severity': 'low', 'category': '神经'},  # 正确
            '脑室扩涨': {'correction': '脑室扩张', 'severity': 'high', 'category': '神经'},
            '脑室扩大': {'correction': '脑室扩张', 'severity': 'medium', 'category': '神经'},
            
            # ==================== 骨关节相关 ====================
            '骨制增生': {'correction': '骨质增生', 'severity': 'high', 'category': '骨骼'},
            '骨刺': {'correction': '骨赘', 'severity': 'medium', 'category': '骨骼'},
            '椎盘突出': {'correction': '椎间盘突出', 'severity': 'high', 'category': '骨骼'},
            
            # ==================== 心血管相关 ====================
            '心影增大': {'correction': '心影增大', 'severity': 'low', 'category': '心血管'},  # 正确
            '心影曾大': {'correction': '心影增大', 'severity': 'high', 'category': '心血管'},
            '主动脉壁钙化': {'correction': '主动脉壁钙化', 'severity': 'low', 'category': '心血管'},  # 正确
            '主动脉壁钙华': {'correction': '主动脉壁钙化', 'severity': 'high', 'category': '心血管'},
        }

        # 加载自定义错别字
        self.custom_typos = self._load_json_file(self.custom_typos_file, {})

        # 加载医学术语白名单
        self.medical_whitelist = self._load_json_file(self.medical_whitelist_file, [])

        # 加载例外规则
        self.exception_rules = self._load_json_file(self.exception_rules_file, [
            {'pattern': r'胸部CT.*未见异常', 'explanation': '正常报告结论', 'type': 'normal'},
            {'pattern': r'头颅MRI.*平扫', 'explanation': '正常检查方法', 'type': 'normal'},
            {'pattern': r'建议.*随访', 'explanation': '正常建议', 'type': 'normal'},
            {'pattern': r'请结合临床', 'explanation': '标准结语', 'type': 'normal'},
            {'pattern': r'双侧.*对称', 'explanation': '正常描述', 'type': 'normal'},
        ])

        # 加载医学模式配置
        medical_config = self._load_json_file(self.medical_patterns_file, {})
        self.inherently_bilateral_organs = medical_config.get('inherently_bilateral_organs', [])
        self.normal_medical_expressions = medical_config.get('normal_medical_expressions', [])
        self.acceptable_variations = medical_config.get('acceptable_variations', {})
        self.contextual_expressions = medical_config.get('contextual_expressions', {})

        # 加载矛盾模式配置
        contradiction_config = self._load_json_file(self.contradiction_patterns_file, {})
        self.contradiction_patterns = contradiction_config.get('contradiction_patterns', [])
        self.normal_medical_patterns = contradiction_config.get('normal_medical_patterns', [])

        # 加载医学连续字符配置
        medical_continuous = self._load_json_file(self.medical_continuous_file, {})
        self.medical_continuous_chars = medical_continuous

        # 加载附加医学词汇
        additional_config = self._load_json_file(self.additional_medical_file, {})
        self.medical_normal_continuous_words = additional_config.get('medical_normal_continuous_words', [])

        # 构建完整的医学正常连续词列表
        self._build_medical_normal_continuous_words()

    def _load_json_file(self, filepath, default_value):
        """加载JSON文件"""
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"加载配置文件失败 {os.path.basename(filepath)}: {e}")

        return default_value

    def _build_medical_normal_continuous_words(self):
        """构建医学正常连续词列表"""
        # 从配置文件中加载的词已经包含在medical_normal_continuous_words中
        # 这里只需要确保完整性
        if not hasattr(self, 'medical_normal_continuous_words') or not self.medical_normal_continuous_words:
            self.medical_normal_continuous_words = []

        # 从medical_continuous_chars中添加词汇
        for char, words in self.medical_continuous_chars.items():
            for word in words:
                if word not in self.medical_normal_continuous_words:
                    self.medical_normal_continuous_words.append(word)

    def _load_learned_patterns(self) -> Dict:
        """加载学习模式"""
        try:
            if os.path.exists(self.learned_patterns_file):
                with open(self.learned_patterns_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data
        except Exception as e:
            print(f"加载学习模式失败: {e}")

        return {'normal_patterns': [], 'false_positives': []}

    def _load_false_positives_from_db(self):
        """从数据库加载误判记录"""
        if not self.db:
            return

        try:
            # 获取学习记录
            records = self.db.get_learning_records(limit=1000)

            # 提取误判记录
            false_positives = []
            for record in records:
                if record['feedback_type'] == 'false_positive':
                    false_positives.append({
                        'original_text': record['original_text'],
                        'feedback_time': record['feedback_time'],
                        'user_comment': record['user_comment']
                    })

            # 更新learned_patterns
            self.learned_patterns['false_positives'] = false_positives

            print(f"从数据库加载了 {len(false_positives)} 条误判记录")

        except Exception as e:
            print(f"从数据库加载误判记录失败: {e}")

    def is_false_positive(self, original_text: str, issue_type: str = None) -> bool:
        """
        检查某个问题是否为误判

        Args:
            original_text: 原始文本片段
            issue_type: 问题类型（可选）

        Returns:
            bool: 是否为误判
        """
        if not original_text:
            return False

        # 检查数据库中的误判记录
        if self.db:
            try:
                records = self.learned_patterns.get('false_positives', [])
                for record in records:
                    record_text = record.get('original_text', '')
                    if not record_text:
                        continue

                    # 多种匹配方式
                    # 1. 精确匹配
                    if original_text == record_text:
                        print(f"✓ 精确匹配误判: {original_text}")
                        return True

                    # 2. 包含匹配（原文包含在误判记录中）
                    if original_text in record_text:
                        print(f"✓ 包含匹配误判: {original_text} in {record_text}")
                        return True

                    # 3. 包含匹配（误判记录包含在原文中）
                    if record_text in original_text:
                        print(f"✓ 反向包含匹配误判: {record_text} in {original_text}")
                        return True

                    # 4. 部分匹配（至少有50%的字符匹配）
                    if len(original_text) >= 5 and len(record_text) >= 5:
                        overlap = len(set(original_text) & set(record_text))
                        if overlap / len(original_text) >= 0.5:
                            print(f"✓ 部分匹配误判: {original_text} vs {record_text} ({overlap}/{len(original_text)})")
                            return True

            except Exception as e:
                print(f"检查误判记录失败: {e}")

        return False

    def check_issue_type_in_false_positives(self, issue_type: str, description: str) -> bool:
        """
        检查特定类型的问题是否在误判记录中

        Args:
            issue_type: 问题类型
            description: 问题描述

        Returns:
            bool: 是否为误判
        """
        if not self.db:
            return False

        try:
            records = self.learned_patterns.get('false_positives', [])
            for record in records:
                user_comment = record.get('user_comment', '')
                # 如果用户评论中包含该问题类型和描述，则认为是误判
                if issue_type and issue_type in user_comment:
                    if description and any(keyword in user_comment for keyword in description.split()[:3]):
                        return True
        except Exception as e:
            print(f"检查问题类型误判失败: {e}")

        return False

    def _save_custom_typos(self):
        """保存自定义错别字"""
        try:
            with open(self.custom_typos_file, 'w', encoding='utf-8') as f:
                json.dump(self.custom_typos, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存自定义错别字失败: {e}")
            return False

    def add_custom_typo(self, typo: str, correction: str) -> bool:
        """添加自定义错别字"""
        if not typo or not correction:
            return False

        self.custom_typos[typo] = {
            'correction': correction,
            'severity': 'medium'
        }

        return self._save_custom_typos()

    def remove_custom_typo(self, typo: str) -> bool:
        """移除自定义错别字"""
        if typo in self.custom_typos:
            del self.custom_typos[typo]
            return self._save_custom_typos()
        return False

    def get_custom_typos(self) -> Dict[str, Dict]:
        """获取所有自定义错别字"""
        return self.custom_typos.copy()

    def add_medical_whitelist(self, term: str) -> bool:
        """添加医学术语到白名单"""
        term = term.strip().upper()
        if term and term not in self.medical_whitelist:
            self.medical_whitelist.append(term)
            return self._save_medical_whitelist()
        return False

    def remove_medical_whitelist(self, term: str) -> bool:
        """从白名单中移除医学术语"""
        term = term.strip().upper()
        if term in self.medical_whitelist:
            self.medical_whitelist.remove(term)
            return self._save_medical_whitelist()
        return False

    def get_medical_whitelist(self) -> list:
        """获取医学术语白名单"""
        return self.medical_whitelist.copy()

    def _save_medical_whitelist(self):
        """保存医学术语白名单"""
        try:
            with open(self.medical_whitelist_file, 'w', encoding='utf-8') as f:
                json.dump(self.medical_whitelist, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存医学术语白名单失败: {e}")
            return False

    def _extract_report_sections(self, text: str) -> Dict[str, str]:
        """提取报告的三个部分：方法、描述、提示"""
        sections = {'方法': '', '描述': '', '提示': ''}

        # 提取检查方法部分
        method_patterns = [
            r'(?:检查方法|检查项目|检查名称|检查部位)[：:\]】](.*?)(?:影像所见|影像描述|检查所见|影像表现|$)',
            r'检查技术[：:\]】](.*?)(?:影像所见|影像描述|检查所见|影像表现|$)',
        ]

        for pattern in method_patterns:
            method_match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if method_match:
                sections['方法'] = method_match.group(1).strip()
                break

        # 提取影像描述部分
        desc_patterns = [
            r'(?:影像所见|影像描述|检查所见|影像表现)[：:\]】](.*?)(?:影像提示|影像结论|诊断意见|结论|$)',
            r'CT所见[：:\]】](.*?)(?:CT提示|诊断意见|结论|$)',
            r'MRI所见[：:\]】](.*?)(?:MRI提示|诊断意见|结论|$)',
        ]

        for pattern in desc_patterns:
            desc_match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if desc_match:
                sections['描述'] = desc_match.group(1).strip()
                break

        # 提取影像提示部分
        prompt_patterns = [
            r'(?:影像提示|影像结论|影像诊断|诊断意见|诊断|结论|印象)\s*[:：\]】]?(.*?)(?:报告医生|审核医生|检查医师|$)',
            r'CT提示\s*[:：\]】]?(.*?)(?:报告医生|审核医生|检查医师|$)',
            r'MRI提示\s*[:：\]】]?(.*?)(?:报告医生|审核医生|检查医师|$)',
        ]

        for pattern in prompt_patterns:
            prompt_match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if prompt_match:
                sections['提示'] = prompt_match.group(1).strip()
                break

        return sections

    def learn_from_feedback(self, word: str, context: str, is_correct: bool):
        """从用户反馈中学习"""
        try:
            if is_correct:
                # 添加到正常模式
                if 'normal_patterns' not in self.learned_patterns:
                    self.learned_patterns['normal_patterns'] = []

                # 创建上下文模式
                pattern = self._create_context_pattern(word, context)
                if pattern not in self.learned_patterns['normal_patterns']:
                    self.learned_patterns['normal_patterns'].append(pattern)
            else:
                # 添加到误报列表
                if 'false_positives' not in self.learned_patterns:
                    self.learned_patterns['false_positives'] = []

                false_positive = f"{word}:{context[:30]}"
                if false_positive not in self.learned_patterns['false_positives']:
                    self.learned_patterns['false_positives'].append(false_positive)

            # 保存学习结果
            self._save_learned_patterns()

        except Exception as e:
            print(f"学习反馈失败: {e}")

    def _create_context_pattern(self, word: str, context: str) -> str:
        """创建上下文模式"""
        # 简化的模式创建：提取上下文中的关键信息
        words_around = re.findall(r'[\u4e00-\u9fff]+', context)

        # 创建包含目标词和周围词的模式
        if words_around:
            # 取前后各两个词
            word_index = words_around.index(word) if word in words_around else -1
            if word_index >= 0:
                start = max(0, word_index - 2)
                end = min(len(words_around), word_index + 3)
                context_words = words_around[start:end]
                return '.*'.join(context_words)

        return f".*{word}.*"

    def _save_learned_patterns(self):
        """保存学习模式"""
        try:
            with open(self.learned_patterns_file, 'w', encoding='utf-8') as f:
                json.dump(self.learned_patterns, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存学习模式失败: {e}")

    def get_similarity_score(self, text1: str, text2: str) -> float:
        """计算两个文本的相似度"""
        # 使用简单的字符串相似度
        return SequenceMatcher(None, text1, text2).ratio()

    def check_acceptability(self, word: str, variation: str) -> bool:
        """检查变体是否可接受"""
        for group in self.acceptable_variations.values():
            if word in group and variation in group:
                return True
        return False
