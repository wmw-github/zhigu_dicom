#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能文本检查模块 v2.6
修复'高信号'等误检测问题，增强无意义字母检测
修正左右检测、信号密度同时出现问题，添加无意义输入检测
增强错别字检测和左右一致性检查，修正误判问题
"""

import re
import json
import os
import sys
from typing import List, Dict
from difflib import SequenceMatcher
from collections import Counter

from .checker_base import BaseTextChecker
from .checker_side import LeftRightChecker
from .checker_typo import TypoChecker
from .checker_logic import LogicChecker
from .checker_grammar import ChineseGrammarChecker
from .checker_critical import CriticalValueDetector

# AI 检测依赖 torch / transformers / sentence-transformers / sklearn / numpy。
# CT3D 是必须兼容 Win7 的绿色嵌入式包，不引入这些重依赖，因此这里做优雅降级：
# 模块缺失时 AI 整条路径静默关闭，仅走规则检测。
try:
    from .checker_ai import AIChecker          # noqa: PLC0415
    _AI_IMPORT_ERROR = None
except Exception as _e:                         # noqa: BLE001
    AIChecker = None
    _AI_IMPORT_ERROR = _e


class _AIStub(object):
    """AI 模块不可用时的占位：所有 AI 能力一律关闭。"""

    ai_enabled = False

    def __init__(self, *args, **kwargs):
        pass

    def check_with_ai(self, *args, **kwargs):
        return {}

    def merge_with_rule_based(self, ai_results, rule_results):
        # 不做任何融合 —— 原实现的 _apply_fusion_logic 存在
        # filtered_logic 初始化后从不 append、却直接覆盖结果的缺陷，
        # 命中 AI 语义问题时会把规则侧 logic_issues 全部清空。
        return rule_results


class SmartTextChecker(BaseTextChecker, LeftRightChecker, TypoChecker, LogicChecker):
    """智能文本检查器 - 整合版"""

    def __init__(self, enable_ai: bool = False, local_model_path: str = None):
        """
        初始化智能文本检查器

        Args:
            enable_ai: 是否启用AI检测。**CT3D 默认 False**（纯规则路线，
                       避免把 torch/sklearn 拖进 Win7 绿色包）。
            local_model_path: 本地语义模型路径（如果提供，将从本地加载）
        """
        BaseTextChecker.__init__(self)
        LeftRightChecker.__init__(self)
        TypoChecker.__init__(self)
        LogicChecker.__init__(self)

        # 初始化中文语法检测器
        self.grammar_checker = ChineseGrammarChecker()

        # 初始化危急值检测器
        self.critical_value_detector = CriticalValueDetector()

        # 自动探测本地模型
        if local_model_path is None:
            # 获取基础目录（适配打包和开发环境）
            if getattr(sys, 'frozen', False):
                base_dir = os.path.dirname(sys.executable)
            else:
                base_dir = os.path.dirname(os.path.abspath(__file__))

            # 尝试查找默认的本地模型路径
            possible_model_dirs = [
                os.path.join(base_dir, 'models', 'bge-small-zh'),
                os.path.join(base_dir, 'models', 'bge-small-zh-medical'),
                os.path.join(base_dir, 'models', 'text2vec-base-chinese'),
                # 兼容上一级目录
                os.path.join(os.path.dirname(base_dir), 'models', 'bge-small-zh'),
                os.path.join(os.path.dirname(base_dir), 'models', 'bge-small-zh-medical'),
            ]
            for p in possible_model_dirs:
                if os.path.exists(p):
                    local_model_path = p
                    break

        # AI 不可用（模块缺失或未找到模型）时退化为桩对象，规则检测不受影响
        if enable_ai and AIChecker is not None:
            try:
                self.ai_checker = AIChecker(enable_ai=enable_ai,
                                            local_model_path=local_model_path)
            except Exception:                      # noqa: BLE001
                self.ai_checker = _AIStub()
        else:
            self.ai_checker = _AIStub()
        self.enable_ai = bool(enable_ai and self.ai_checker.ai_enabled)

    def apply_user_rules(self, custom_typos=None, extra_terms=None):
        """把用户在设置窗里加的规则注入检查器（热更新，不用重启程序）。

        为什么需要显式注入
        ------------------
        checker 实例由 controller 缓存复用（每次检测省掉约 0.3 秒规则加载）。
        用户新增错别字后如果不注入，下一次检测用的还是旧规则集 —— 表现就是
        「加了词但没用」。这个方法是唯一的注入口，pipeline 每次运行前都会调。

        Parameters
        ----------
        custom_typos : dict, optional
            ``{错词: {"correction": 正确写法, "severity": "high|medium|low"}}``。
        extra_terms : list of str, optional
            用户补充的医学术语白名单（英文缩写等），避免被判成乱码。

        Notes
        -----
        **每次都先恢复到基线再重新应用**，不是增量 append。否则用户在设置里
        删掉一个错别字/术语后，缓存里的旧规则还在 → 表现是「删了没用」。
        基线在第一次调用时快照（此时 checker 刚从配置文件加载完）。
        """
        # 1) 回到基线（首次调用时快照）
        if not hasattr(self, "_base_rules_snapshot"):
            self._base_rules_snapshot = (dict(self.custom_typos),
                                         list(self.medical_whitelist))
        else:
            base_typos, base_terms = self._base_rules_snapshot
            self.custom_typos = dict(base_typos)
            self.medical_whitelist = list(base_terms)
            self._whitelist_cache = None          # 让 typo 模块重建白名单 set

        # 2) 应用当前规则
        if custom_typos:
            for word, data in custom_typos.items():
                word = str(word or "").strip()
                if not word:
                    continue
                if isinstance(data, dict):
                    corr = str(data.get("correction") or "").strip()
                    sev = str(data.get("severity") or "medium").lower()
                else:
                    corr, sev = str(data or "").strip(), "medium"
                if not corr:
                    continue                      # 没有正确写法就不是错别字规则
                self.custom_typos[word] = {"correction": corr, "severity": sev}

        if extra_terms:
            for term in extra_terms:
                term = str(term or "").strip()
                if term:
                    self.medical_whitelist.append(term)
            self._whitelist_cache = None

    def check_with_intelligence(self, text: str, gender: str = '未知',
                                check_method: str = '未知', report_type: str = '未知') -> Dict:
        """
        智能检查入口 - 调整检查顺序和敏感度
        """
        results = {
            'typos': [],
            'repeated_words': [],  # 取消重复词检测，保留字段但始终为空
            'logic_issues': [],
            'semantic_issues': [],
            'contextual_issues': [],
            'warnings': [],
            'suggestions': [],
            'critical_values': []  # 新增：危急值检测结果
        }

        # 提取报告的三个主要部分：检查方法、影像所见、影像提示
        report_sections = self._extract_report_sections(text)
        
        # 只检查这三个部分的文本，忽略其他信息（如患者基本信息、设备信息等）
        sections_to_check = [
            report_sections['方法'],
            report_sections['描述'],
            report_sections['提示']
        ]
        
        # 将三个部分的文本组合成一个新的文本，用于检查
        text_to_check = '\n'.join(sections_to_check)
        
        # 如果没有提取到任何部分，使用原始文本作为备选
        if not text_to_check.strip():
            text_to_check = text

        # 0. 首先检查无意义字母组合
        meaningless_warnings = self._check_meaningless_input(text_to_check)
        results['warnings'].extend(meaningless_warnings)
        unit_warnings = self._check_units(text_to_check)
        results['warnings'].extend(unit_warnings)

        # 1. 检查例外规则（如果属于正常模式，只用于内部逻辑，不再作为警告提示）
        exception_check = self._check_exception_rules(text_to_check)

        # 2. 智能错别字检查（高优先级）- 增强版
        results['typos'] = self.smart_check_typos_enhanced(text_to_check)

        # 3. 取消重复词检测功能
        results['repeated_words'] = []

        # 4. 智能逻辑检查（高优先级）
        logic_issues = self.smart_check_logic(text_to_check, gender, check_method, report_type)

        # 过滤掉正常医学描述中的"假矛盾"
        results['logic_issues'] = self._filter_false_contradictions(logic_issues, text_to_check)

        # 5. 检查信号和密度同时出现的问题
        signal_density_issues = self._check_signal_density_conflict(text_to_check, check_method, report_type)
        results['logic_issues'].extend(signal_density_issues)

        # 6. 检查左右侧矛盾（特别检查标题与内容的一致性）
        side_consistency_issues = self._check_side_title_consistency(text)
        results['logic_issues'].extend(side_consistency_issues)

        # 7. 语义合理性检查（中优先级）
        results['semantic_issues'] = self.check_semantic_issues(text_to_check)

        # 8. 上下文一致性检查（低优先级）
        context_issues = self.check_context_consistency(text_to_check, report_type)
        results['contextual_issues'] = context_issues

        # 9. 中文语法检测（新增）
        grammar_errors = self.grammar_checker.check_grammar(text_to_check)
        # 将语法错误归类到 logic_issues 中
        for err in grammar_errors:
            results['logic_issues'].append({
                'type': err['type'],
                'text': err['word'],
                'description': err['description'],
                'severity': err['severity'],
                'suggestion': err.get('correction', '')
            })

        # 10. AI检测（如果启用）
        if self.enable_ai and self.ai_checker.ai_enabled:
            print("正在进行AI智能检测...")
            ai_results = self.ai_checker.check_with_ai(
                text_to_check, 
                report_type, 
                gender, 
                check_method
            )
            
            # 融合AI检测结果和规则检测结果
            results = self.ai_checker.merge_with_rule_based(ai_results, results)
        else:
            # AI未启用，添加提示
            if self.enable_ai:
                results['warnings'].append("AI检测功能不可用，仅使用规则检测")

        # 11. 生成智能建议
        results['suggestions'] = self.generate_intelligent_suggestions(results, text_to_check)

        # 12. 危急值检测（新增）
        critical_findings = self.critical_value_detector.detect_critical_values(
            text=text_to_check
        )
        results['critical_values'] = critical_findings

        return results

    def smart_check_logic(self, text: str, gender: str, check_method: str, report_type: str) -> List[Dict]:
        """智能逻辑检查"""
        issues = []

        report_type_upper = report_type.upper()
        check_method_upper = check_method.upper()

        # 1. 检查报告类型与术语的智能匹配
        issues.extend(self._smart_check_terminology(text, report_type_upper, check_method_upper))

        # 2. 智能性别器官检查
        issues.extend(self._smart_check_gender_organs(text, gender))

        # 3. 智能左右一致性检查（只在描述不一致时提醒）
        issues.extend(self._smart_check_left_right_consistency(text))

        # 4. 智能矛盾表达检查（最后进行，避免干扰）
        # 先检查是否是正常的医学描述
        if not self._is_normal_full_description(text):
            issues.extend(self._check_contradictions(text))

        return issues


# 兼容旧版本的TextChecker类
class TextChecker(SmartTextChecker):
    """兼容旧版本的文本检查器"""

    def check_typos(self, text: str) -> List[Dict]:
        """兼容旧版本的错别字检查方法"""
        results = self.smart_check_typos(text)
        # 转换为旧版本格式
        return results

    def check_repeated_words(self, text: str) -> List[str]:
        """兼容旧版本的重复词检查方法 - 已取消该功能"""
        return []  # 直接返回空列表

    def check_logic_issues(self, text: str, gender: str, check_method: str, report_type: str = '未知') -> List[Dict]:
        """兼容旧版本的逻辑问题检查方法"""
        results = self.smart_check_logic(text, gender, check_method, report_type)
        return results
