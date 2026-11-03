#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能文本检查器 - 错别字和无意义输入检测模块
"""

import re
from typing import List, Dict

from .checker_base import BaseTextChecker


class TypoChecker:
    """错别字和无意义输入检测模块"""

    def __init__(self):
        """初始化错别字检查器"""
        # 创建BaseTextChecker实例作为成员变量(组合而非继承)
        from .checker_base import BaseTextChecker
        self._base_checker = BaseTextChecker()

        # 继承BaseTextChecker的属性
        self.preset_typos = self._base_checker.preset_typos
        self.custom_typos = self._base_checker.custom_typos
        self.medical_whitelist = self._base_checker.medical_whitelist
        self.db = self._base_checker.db

        # 无意义字母组合模式 - 增强版
        self.meaningless_letter_patterns = [
            # 增强：检测任意连续英文字母（2个或更多），提高无意义字母检测能力
            r'\b[a-zA-Z]{2,}\b',  # 2个或更多连续英文字母作为独立词
            r'(?<!\w)[a-zA-Z]{3,}(?!\w)',  # 3个或更多连续英文字母，确保不是词的一部分
            r'[a-zA-Z]{4,}',  # 4个或更多连续英文字母
            r'dssda',  # 特定的无意义组合
            r'asdf',   # 常见的键盘输入
            r'qwer',   # 常见的键盘输入
            r'zxcv',   # 常见的键盘输入
            r'(.)\1{2,}',  # 同一个字符重复3次或更多
            # 新增常见随机字母组合
            r'hh+',    # 重复的h
            r'kk+',    # 重复的k
            r'll+',    # 重复的l
            r'aa+',    # 重复的a
            r'bb+',    # 重复的b
            r'cc+',    # 重复的c
            r'dd+',    # 重复的d
            r'ee+',    # 重复 of e
            r'ff+',    # 重复 of f
            r'gg+',    # 重复 of g
        ]

        # 医学缩写白名单 - 增强版（提升为类属性）
        self.medical_abbreviations = [
            # 基础医学影像术语
            'CT', 'MRI', 'DR', 'DWI', 'ADC', 'FOV', 'TR', 'TE',
            'STIR', 'T1WI', 'T2WI', 'PDWI', 'FLAIR', 'MRA', 'MRV',
            'HU', 'KV', 'MA', 'MS', 'S', 'CM', 'MM', 'X线', 'PICC',
            # 常见医学序列术语
            'T1', 'T2', 'PD', 'TI', 'BOLD', 'EPI', 'GRE', 'SE',
            'IR', 'SPGR', 'MPR', 'MIP', 'VR', 'CPR', 'STS',
            # 肿瘤相关缩写
            'CA', 'ICU', 'FS', 'FS-PD', 'FS-T2', 'PDFS', 'DSA',
            # 血管成像和其他缩写
            'CTA', 'IM', 'CTA', 'VR', 'MIP', 'CPR', 'MPVR', 'IM',
            # 扩展医学序列和技术
            'T1W', 'T2W', 'T2FS', 'PDW', 'PDFS', 'ADC图', 'DWI图',
            # 脊柱和解剖分段
            'L1', 'L2', 'L3', 'L4', 'L5', 'C1', 'C2', 'C3', 'C4', 
            'C5', 'C6', 'C7', 'T1', 'T2', 'T3', 'T4', 'T5', 'T6', 
            'T7', 'T8', 'T9', 'T10', 'T11', 'T12', 'S1', 'S2', 'S3', 
            'S4', 'S5', 'S6',
            # 其他常见医学缩写和罗马数字
            'XRAY', 'CR', 'US', 'ECG', 'PET', 'SPECT', 'MRCP', 'MRU',
            'Gd', 'Gd-DTPA', 'ROI', 'SUV', 'ETL', 'NEX', 'FSE',
            'HIZ', 'DAS', 'III', 'II', 'IV', 'VI', 'VII', 'VIII',
            # 心脏相关
            'Cine', 'CineMRI', 'MRCCP', 'AVM',
            # 肝脏分段
            'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8',
            # 常见的中文+字母组合
            'T1高信号', 'T2低信号', 'T2WI高信号', 'T1WI等信号',
            'DWI高信号', 'ADC低信号', 'FLAIR高信号', 'T2FS低信号',
            'L3-L4', 'L4-L5', 'C5-C6', 'C6-C7', 'T1-T2',
            '肝S6', '肝S8', '肺S1', '肺S3',
            '斑片状T1', '结节状T2', '条片状FLAIR', '团块状DWI',
            # 核医学相关缩写
            'PET-CT', 'ECT', 'SPECT-CT', 'RUS-CHN', 'RUS', 'CHN',
            'PET', 'SPECT', 'ECT', 'RUS', 'CHN', 'PETCT', 'ECTCT',
            'SPECTCT', 'RUSCHN',
            # 功能性成像缩写
            'fMRI', 'DTI', 'SWI', 'ASL', 'PERF', 'BOLD', 'EPI',
            'MRS', 'fMRI', 'DTI', 'SWI', 'ASL', 'PERF',
            # 心电图和脑电图相关
            'ECG', 'EEG', 'EMG', 'EP', 'VEP', 'BAEP', 'SEP',
            # 实验室检查缩写
            'WBC', 'RBC', 'HGB', 'HCT', 'PLT', 'MCV', 'MCH', 'MCHC',
            'ALT', 'AST', 'ALP', 'GGT', 'TBIL', 'DBIL', 'IBIL',
            'TP', 'ALB', 'GLB', 'A/G', 'BUN', 'CR', 'UA', 'GLU',
            'TG', 'TC', 'HDL-C', 'LDL-C', 'VLDL-C',
            # 凝血功能相关
            'PT', 'APTT', 'TT', 'FIB', 'INR',
            # 内分泌相关
            'T3', 'T4', 'FT3', 'FT4', 'TSH', 'ACTH', 'GH', 'PRL',
            'FSH', 'LH', 'E2', 'P', 'T', 'CORT', 'INS', 'C-P',
            # 肿瘤标志物
            'AFP', 'CEA', 'CA125', 'CA153', 'CA199', 'CA724', 'CYFRA21-1',
            'NSE', 'SCC', 'PSA', 'FPSA', 'HE4', 'CA50', 'CA242',
            # 感染标志物
            'CRP', 'ESR', 'PCT', 'WBC', 'NEUT', 'LYM', 'MONO',
            # 血型相关
            'A型', 'B型', 'AB型', 'O型', 'RH+', 'RH-',
            # 药物相关缩写
            'IV', 'PO', 'IM', 'SC', 'PRN', 'QD', 'BID', 'TID', 'QID',
            'QN', 'HS', 'AC', 'PC', 'STAT', 'NS', 'GS', 'D5W', 'D10W',
            # 医学机构标准
            'WHO', 'FDA', 'NMPA', 'CFDA', 'EMA', 'PMDA', 'TGA',
            # 骨龄相关标准
            'TW2', 'TW3', 'CHN', 'RUS', 'Greulich', 'Pyle', 'Tanner',
            'TW2-C', 'TW2-R', 'TW2-T', 'TW3-C', 'TW3-R', 'TW3-T',
            'RUS-CHN', 'CHN-RUS', 'TW2-CHN', 'TW3-CHN',
            # 影像技术参数
            'SNR', 'CNR', 'TE', 'TR', 'TI', 'FA', 'ETL', 'NEX',
            'BW', 'FOV', 'THK', 'GAP', 'MAT', 'IPAT', 'SENSE',
            # 常见疾病缩写
            'COPD', 'CHD', 'CAD', 'HTN', 'DM', 'T2DM', 'T1DM',
            'RA', 'SLE', 'AS', 'OA', 'OP', 'DVT', 'PE', 'AMI',
            'CVA', 'TIA', 'ICH', 'SAH', 'AD', 'PD', 'ALS',
            # 器官功能分级
            'NYHA', 'Killip', 'Child-Pugh', 'MELD', 'APACHE',
            # 手术相关缩写
            'OR', 'ICU', 'CCU', 'NICU', 'PICU', 'SICU', 'ER',
            'OPD', 'IPD', 'ED', 'OT', 'PT', 'RT', 'CT',
            # 其他常见医学缩写
            'BMI', 'BP', 'HR', 'RR', 'SpO2', 'T', 'P', 'R', 'BP',
            'IVP', 'UGI', 'BE', 'HSG', 'ERCP', 'MRCP', 'PTCD',
            'TIPS', 'PCI', 'CABG', 'PTCA', 'IABP', 'ECMO', 'CRRT',
            'HD', 'PD', 'CAPD', 'APD', 'TPN', 'EN', 'PN',
        ]

        # 初始化自定义错别字：**config/custom_typos.json 优先**，
        # 硬编码仅作兜底。原实现在这里用一个 2 条的 dict 无条件覆盖，
        # 把 _base_checker 刚从 JSON 载入的 34 条规则全部丢弃（死配置）。
        _default_typos = {
            "粘膜": {"correction": "黏膜", "severity": "medium"},
            "肌建": {"correction": "肌腱", "severity": "high"},
        }
        _loaded = self._base_checker.custom_typos
        if isinstance(_loaded, dict) and _loaded:
            merged = dict(_loaded)
            for _k, _v in _default_typos.items():
                merged.setdefault(_k, _v)
            self.custom_typos = merged
        else:
            self.custom_typos = _default_typos

    # ------------------------------------------------------------------
    # 医学术语 vs 乱码的判定（2026-10-27 重写）
    # ------------------------------------------------------------------
    #
    # 原来的判据是「不在硬编码白名单里就报无意义输入」。这个策略对影像报告
    # 是错的：报告里拉丁字母缩写密度极高（ECT / PICC / TACE / VATS /
    # BI-RADS / Gd-DTPA ...），白名单永远补不完，实测 ECT、TACE、VATS、
    # DTPA、RADS 全部被误报成「随机误输入字母」，而且是 medium 级（黄灯），
    # 直接影响可用性。
    #
    # 现在改成**举证式判定**：默认认为拉丁字母串是医学术语，只有出现明确的
    # 乱码证据（键盘序列、连续重复字符、无元音长串）才报，并且降为 low。
    # 判断依据是「影像报告里的英文几乎都是缩写或序列名」，而不是反过来的
    # 「默认是乱码」。

    _ROMAN_AND_SEGMENTS = frozenset(
        ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X', 'XI', 'XII']
        + ['L%d' % i for i in range(1, 7)]
        + ['C%d' % i for i in range(1, 9)]
        + ['T%d' % i for i in range(1, 13)]
        + ['S%d' % i for i in range(1, 9)]
    )

    # 键盘上连续按键的痕迹
    _KEYBOARD_ROWS = ('qwertyuiop', 'asdfghjkl', 'zxcvbnm')
    _CONSONANTS = 'bcdfghjklmnpqrstvwxyz'

    def _abbr_set(self):
        """内置医学缩写表的 set 缓存（100+ 项线性查太慢，这里缓存）。"""
        cached = getattr(self, '_abbr_cache', None)
        if cached is None:
            cached = set()
            for a in (getattr(self, 'medical_abbreviations', None) or []):
                cached.add(str(a).strip().upper())
            self._abbr_cache = cached
        return cached

    def _user_whitelist_set(self):
        """用户白名单（设置窗可加）的 set 缓存。"""
        cached = getattr(self, '_whitelist_cache', None)
        if cached is None:
            cached = set()
            for a in (getattr(self, 'medical_whitelist', None) or []):
                cached.add(str(a).strip().upper())
            self._whitelist_cache = cached
        return cached

    def _is_keyboard_sequence(self, word):
        """是否像键盘连按（asdf / qwer / zxcv / hjkl 等）。"""
        low = word.lower()
        for row in self._KEYBOARD_ROWS:
            for i in range(len(row) - 2):
                if row[i:i + 3] in low:
                    return True
        return False

    def _is_known_medical_term(self, word):
        """这个词够不够格被当作医学术语，从而不必报乱码。

        依次放行：内置缩写表 → 用户白名单 → 罗马数字/脊柱分段 →
        全大写 2~6 字母（缩写几乎全大写；键盘连按和连续重复除外）。
        """
        u = word.strip().upper()
        if not u:
            return True
        if u in self._abbr_set():
            return True
        if u in self._user_whitelist_set():
            return True
        if u in self._ROMAN_AND_SEGMENTS:
            return True
        # 全大写 2~6 字母：典型缩写形态（ECT / PICC / TACE / VATS / RADS）。
        # 但键盘连按（ASDF）和连续重复（AAA）仍要放行到乱码判定里去。
        if 2 <= len(u) <= 6 and word.isupper():
            if not self._is_keyboard_sequence(word) and not re.search(r'(.)\1{2,}', word):
                return True
        return False

    def _looks_like_typo_noise(self, word):
        """是否有「明确乱码证据」。默认 False（即默认信任为术语）。"""
        if self._is_keyboard_sequence(word):
            return True
        low = word.lower()
        # 连续 3 个以上相同字符（AAA / sss）
        if re.search(r'(.)\1{2,}', word):
            return True
        # 全元音（aeiou / aiaia）
        if re.fullmatch(r'[aeiou]+', low) and len(low) >= 3:
            return True
        # 连续 4 个以上辅音（dssd / hjkl）
        if re.search(r'[%s]{4,}' % self._CONSONANTS, low):
            return True
        return False

    def _check_meaningless_input(self, text: str) -> List[str]:
        """检查无意义的字母组合（举证式：只报有明确乱码证据的串）。"""
        warnings = []

        # 增强：提取所有字母序列进行更精确的检查
        all_letter_sequences = re.findall(r'[a-zA-Z]{2,}', text)

        for match in all_letter_sequences:
            # 对于长度小于3的字母序列，一般不作为无意义输入告警，降低误报
            if len(match) < 3:
                continue

            # 已知医学术语 / 用户白名单 / 缩写形态 → 直接放行
            if self._is_known_medical_term(match):
                continue
            # 剩下的必须有明确乱码证据才报（举证式，见 _looks_like_typo_noise）
            if not self._looks_like_typo_noise(match):
                continue
            # 出现在「TR 2500 / 层厚 5mm」这类参数上下文里，不算乱码
            if self._is_reasonable_context(text, match):
                continue

            is_keyboard_pattern = self._is_keyboard_sequence(match)
            is_repeat_pattern = len(set(match.upper())) == 1 and len(match) >= 2

            if is_keyboard_pattern:
                warning_msg = f"检测到可能的键盘输入模式: '{match}'"
            elif is_repeat_pattern:
                warning_msg = f"检测到重复字母: '{match}'"
            else:
                warning_msg = f"检测到可能的无意义输入: '{match}'"

                warnings.append(warning_msg)

        return warnings

    def _is_keyboard_pattern(self, text: str) -> bool:
        """检查是否是键盘输入模式（如asdf, qwer, zxcv等）"""
        keyboard_patterns = [
            'asdf', 'qwer', 'zxcv', 'uiop', 'jkl', 'bnm',
            'asd', 'qwe', 'zxc', 'iop', 'jkl', 'bnm',
            'dfgh', 'rtyu', 'cvbn', 'tyui', 'ghjk', 'vbnm'
        ]

        text_lower = text.lower()
        for pattern in keyboard_patterns:
            if pattern in text_lower or text_lower in pattern:
                return True

        # 检查是否是连续的键盘按键（按键盘顺序）
        if len(text) >= 3:
            # 检查QWERTY键盘行顺序
            keyboard_rows = [
                'qwertyuiop',
                'asdfghjkl',
                'zxcvbnm'
            ]

            for row in keyboard_rows:
                if text_lower in row:
                    return True

        return False

    def _looks_like_random_letters(self, text: str) -> bool:
        """检查是否是随机字母组合"""
        if len(text) < 3:
            return False

        text_lower = text.lower()
        text_upper = text.upper()

        # 首先检查是否在医学缩写白名单中
        for abbr in self.medical_abbreviations:
            if text_upper == abbr.upper():
                return False
        
        # 检查是否在用户自定义白名单中
        if hasattr(self, 'medical_whitelist'):
            for abbr in self.medical_whitelist:
                if text_upper == abbr.upper():
                    return False

        # 排除白名单中的词，如 III
        common_terms = ['III', 'II', 'IV', 'VI', 'VII', 'VIII', 'DSA', 'DAS']
        if text_upper in common_terms:
            return False

        # 常见随机字母模式
        random_patterns = [
            r'^[aeiou]{3,}$',  # 全是元音字母
            r'^[bcdfghjklmnpqrstvwxyz]{3,}$',  # 全是辅音字母
            r'^[hjkl]{3,}$',  # 常见随机字母组合
            r'^[qwrtpsdfghjklzxcvbnm]{3,}$',  # 无元音字母
        ]

        for pattern in random_patterns:
            if re.match(pattern, text_lower):
                return True

        # 检查字母分布是否均匀（随机特征）
        if len(text) >= 4:
            # 计算不同字母的比例
            unique_chars = len(set(text_lower))
            if unique_chars / len(text) < 0.5:  # 重复字母过多
                return True

        return False

    def _is_reasonable_context(self, text: str, match: str) -> bool:
        """检查是否在合理的上下文中"""
        # 查找匹配的位置
        match_lower = match.lower()
        text_lower = text.lower()

        # 检查匹配前后的文本
        start_pos = text_lower.find(match_lower)
        if start_pos == -1:
            return False

        end_pos = start_pos + len(match)

        # 获取上下文（前后各20个字符）
        context_start = max(0, start_pos - 20)
        context_end = min(len(text), end_pos + 20)
        context = text[context_start:context_end]

        # 合理的上下文模式
        reasonable_patterns = [
            # 检查方法中的参数
            r'TR.*\d+',
            r'TE.*\d+',
            r'层厚.*\d+',
            r'间距.*\d+',
            r'参数.*[a-zA-Z]',

            # 序列名称
            r'T1WI',
            r'T2WI',
            r'DWI',
            r'ADC',
            r'FLAIR',
            r'STIR',

            # 单位
            r'\d+ms',  # 如 100ms
            r'\d+cm',  # 如 256cm
            r'\d+mm',  # 如 10mm
            r'\d+kV',  # 如 120kV
            r'\d+mA',  # 如 200mA,

            # 新增合理上下文
            r'序列.*名称',
            r'扫描.*参数',
            r'检查.*技术',
            r'成像.*参数',
            r'设备.*型号',
            r'软件.*版本',
            r'结合.*进一步',
            r'进一步.*检查',
            r'建议.*检查',
        ]

        for pattern in reasonable_patterns:
            if re.search(pattern, context, re.IGNORECASE):
                return True

        # 检查是否在检查方法或技术描述中
        method_contexts = ['检查方法', '检查技术', '扫描参数', '序列', '技术', '参数', '设备', '软件']
        for method_ctx in method_contexts:
            if method_ctx in context:
                return True

        return False

    def smart_check_typos_enhanced(self, text: str) -> List[Dict]:
        """增强版智能错别字检查 - 包含部分匹配"""
        found_typos = []
        
        # 合并预设错别字和自定义错别字
        all_typos = {}
        if hasattr(self, 'preset_typos'):
            all_typos.update(self.preset_typos)
        if hasattr(self, 'custom_typos'):
            all_typos.update(self.custom_typos)
        
        # 从数据库加载最新的错别字（如果有）
        if hasattr(self, 'db') and self.db:
            try:
                db_typos = self.db.get_custom_typos()
                for typo_item in db_typos:
                    typo = typo_item['typo']
                    if typo not in all_typos:  # 不覆盖已有的
                        all_typos[typo] = {
                            'correction': typo_item['correction'],
                            'severity': typo_item['severity']
                        }
            except Exception as e:
                print(f"从数据库加载错别字失败: {e}")
        
        # 分句检查
        sentences = re.split(r'[。！？；\n]', text)

        for sentence in sentences:
            if len(sentence.strip()) < 2:
                continue

            # 1. 直接匹配错别字
            for typo, data in all_typos.items():
                # 跳过信号相关的部分匹配（这些由partial_patterns处理）
                if typo in ['高信', '低信', '等信']:
                    continue

                # 直接匹配错别字，不检查前后是否是中文
                if typo in sentence:
                    # 检查是否为误判（使用上下文匹配）
                    is_fp = False
                    if hasattr(self, 'is_false_positive'):
                        # 检查错别字本身是否为误判
                        if self.is_false_positive(typo):
                            print(f"✓ 错别字 '{typo}' 已标记为误判，跳过")
                            is_fp = True

                        # 检查句子片段是否为误判
                        # 提取包含错别字的句子片段（前后各加10个字符）
                        typo_pos = sentence.find(typo)
                        start = max(0, typo_pos - 10)
                        end = min(len(sentence), typo_pos + len(typo) + 10)
                        context_fragment = sentence[start:end]

                        if self.is_false_positive(context_fragment):
                            print(f"✓ 句子片段 '{context_fragment}' 已标记为误判，跳过")
                            is_fp = True

                    if is_fp:
                        continue  # 跳过已标记为误判的错别字

                    found_typos.append({
                        'word': typo,
                        'correction': data['correction'],
                        'severity': data.get('severity', 'medium'),
                        'context': sentence[:50] + '...' if len(sentence) > 50 else sentence,
                        'confidence': 0.9,
                        'type': '直接匹配'
                    })

            # 2. 修复：彻底移除“信/信号”相关的部分匹配误报，改由更精确的语义检测处理
            # 移除所有可能导致“信”或“高信”等误报的规则
            partial_patterns = [
                # 如果用户明确需要检测这类不完整描述，可以改用更严格的正则，但目前为了降低误报，全部移除
            ]

            for pattern, found, correction, description in partial_patterns:
                matches = re.findall(pattern, sentence)
                if matches:
                    found_typos.append({
                        'word': found,
                        'correction': correction,
                        'severity': 'high',
                        'context': sentence[:50] + '...' if len(sentence) > 50 else sentence,
                        'confidence': 0.8,
                        'type': '部分匹配'
                    })

            # 3. 专门检查肌腱相关的错别字
            tendon_patterns = [
                # 检查"肌建"在医学上下文中
                (r'(?<![a-zA-Z\u4e00-\u9fff])肌建(?![a-zA-Z\u4e00-\u9fff])', '肌建', '肌腱', '肌腱错别字'),
                # 检查"三头肌建"、"二头肌建"等
                (r'[二三]头肌建\b', '三头肌建', '三头肌腱', '肌腱错别字'),
                # 检查"跟建"、"跟踺"等
                (r'跟建\b', '跟建', '跟腱', '跟腱错别字'),
                (r'跟踺\b', '跟踺', '跟腱', '跟腱错别字'),
                # 检查"踺"在肌腱相关的词语中
                (r'[肌跟]踺\b', '踺', '腱', '肌腱错别字'),
            ]

            for pattern, found, correction, description in tendon_patterns:
                if re.search(pattern, sentence):
                    found_typos.append({
                        'word': found,
                        'correction': correction,
                        'severity': 'high',
                        'context': sentence[:50] + '...' if len(sentence) > 50 else sentence,
                        'confidence': 0.8,
                        'type': '肌腱错别字'
                    })

            # 4. 检测随机误输入的字母
            # 4.1 检测连续的英文字母序列（3个或以上）
            #
            # 判据与 _check_meaningless_input 统一（2026-10-27）：先放行已知
            # 医学术语和「全大写 2~6 字母」这类缩写形态，剩下要有明确乱码
            # 证据才报，且降为 low —— 这是「提示」级，不该当成错别字那种
            # 需要修改的问题。原来这里是硬编码白名单反查 + medium 级，
            # 实测把 ECT / TACE / VATS / DTPA / RADS 全误报成错别字。
            words = re.findall(r'[a-zA-Z]+', sentence)
            for word in words:
                if len(word) < 3:
                    continue
                if self._is_known_medical_term(word):
                    continue
                if not self._looks_like_typo_noise(word):
                    continue
                if self._is_reasonable_context(sentence, word):
                    continue

                found_typos.append({
                    'word': word,
                    'correction': '',
                    'severity': 'low',
                    'context': sentence[:50] + '...' if len(sentence) > 50 else sentence,
                    'confidence': 0.5,
                    'type': '随机误输入字母'
                })

            # 4.2 检测中文词后面跟着的单个或多个英文字母
            # 改进：排除正常的医学表达式，如"矢状面T1WI"、"斑片状T1高信号"等
            chinese_with_letters_pattern = r'([\u4e00-\u9fff]+)([a-zA-Z]+)'
            chinese_with_letters_matches = re.findall(chinese_with_letters_pattern, sentence)
            for chinese_part, letters_part in chinese_with_letters_matches:
                # 字母本身是已知术语（「肝癌TACE术后」「右上肢PICC置管」）→
                # 中文紧挨术语是正常写法，不是「中文后误输入字母」。
                if self._is_known_medical_term(letters_part):
                    continue
                # 该字母串若已由 4.1 判为乱码，这里不再重复报。
                # 否则同一个 'asdf' 会以「双肺纹理asdf」「双肺纹理asdf清晰」
                # 等好几种中文前缀各报一条，同一次检测里堆 3 条重复问题。
                if (len(letters_part) >= 3
                        and not self._is_known_medical_term(letters_part)
                        and self._looks_like_typo_noise(letters_part)):
                    continue
                # 检查是否是正常的医学表达式
                full_term = (chinese_part + letters_part).upper()
                # 排除常见的医学表达式格式
                is_medical_term = False
                
                # 检查是否是类似"T1"、"T2"、"FLAIR"等序列术语，前面可能跟着形容词
                letters_upper = letters_part.upper()
                
                # 1. 检查字母后面是否跟着数字或其他医学术语
                # 提取整个句子，检查字母后面的内容
                letters_pos = sentence.find(letters_part)
                if letters_pos != -1 and letters_pos + len(letters_part) < len(sentence):
                    next_char = sentence[letters_pos + len(letters_part)]
                    # 如果字母后面跟着数字，很可能是医学术语（如T1、T2、L3等）
                    if next_char.isdigit():
                        is_medical_term = True
                    # 如果字母后面跟着其他医学术语的一部分
                    elif next_char in ['W', 'I', '-', 'S', 'L', 'C', 'T', 'R', 'E']:
                        is_medical_term = True
                
                # 2. 检查是否是常见的医学序列术语
                if not is_medical_term and letters_upper in ['T1', 'T2', 'T1W', 'T2W', 'T1WI', 'T2WI', 'FLAIR', 'DWI', 'ADC', 'STIR', 'GRE', 'SE', 'EPI', 'FS', 'FS-PD', 'FS-T2', 'PD', 'PDWI', 'PDFS', 'T2FS']:
                    is_medical_term = True
                
                # 3. 检查是否是脊柱节段或肝脏分段
                elif not is_medical_term and re.match(r'^[LCTS][1-9]\d?$', letters_upper):
                    is_medical_term = True
                
                # 4. 检查是否是常见的医学缩写
                elif not is_medical_term and letters_upper in ['CT', 'MRI', 'DR', 'US', 'ECG', 'PET', 'SPECT', 'CTA', 'MRA', 'DSA', 'XRAY', 'CR', 'VR', 'IM', 'CA', 'ICU', 'ROI', 'FOV', 'SUV', 'TR', 'TE', 'TI', 'FA', 'ETL', 'NEX', 'FSE', 'MPR', 'MIP', 'AVM', 'SET', 'Cine', 'CineMRI', 'MRCCP', 'HIZ', 'DAS', 'III', 'II', 'IV', 'VI', 'VII', 'VIII', 'PICC']:
                    is_medical_term = True
                
                # 5. 检查是否是形容词+字母的模式，如"斑片状T"、"结节状T"等
                elif not is_medical_term:
                    # 常见的医学形容词
                    medical_adjectives = ['斑片状', '结节状', '团块状', '条片状', '片状', '斑片', '结节', '团块', '条片',
                                         '不规则', '规则', '类圆形', '圆形', '椭圆形', '球形', '半圆形',
                                         '等密度', '低密度', '高密度', '混杂密度',
                                         '高信号', '低信号', '等信号', '混杂信号',
                                         '均匀', '不均匀', '清晰', '模糊', '不清',
                                         '轻度', '中度', '重度', '明显', '显著',
                                         '多发', '单发', '散在', '局限', '弥漫',
                                         '左侧', '右侧', '双侧', '单侧',
                                         '前部', '后部', '上部', '下部', '内侧', '外侧', '中央', '周边',
                                         '浅表', '深部', '远端', '近端', '邻近', '旁',
                                         '囊性', '实性', '囊实性', '脂肪密度', '钙化', '骨化',
                                         '增厚', '变薄', '增大', '缩小', '变形', '移位',
                                         '膨出', '突出', '脱出', '游离', '疝出',
                                         '强化', '轻度强化', '中度强化', '明显强化', '不均匀强化', '环形强化']
                    
                    # 检查chinese_part是否是医学形容词
                    if any(adj in chinese_part for adj in medical_adjectives):
                        is_medical_term = True
                
                # 6. 检查是否是类似"肝S"、"平L"、"见T"等常见医学表达
                elif not is_medical_term and letters_upper in ['S', 'L', 'C', 'T', 'VR', 'IM']:
                    common_medical_patterns = ['见', '位于', '约平', '平', '肝', '肺', '左侧', '右侧', '双侧',
                                             '见条片状', '部分', '大者位于', '考虑', '考虑为',
                                             '部分椎体见', '椎体相对缘见', '局部', '病灶', '其内',
                                             '矢状面', '横断面', '冠状面', '呈', '可见', '亦可见',
                                             '可', '中央', '液化', '呈长', '及长', '内可见',
                                             '底部见一', '显示更长', '及更长', '其中可见', '及',
                                             '可见更长', '水抑制', '肿块', '中央可见液化坏死区呈',
                                             '长', '失状面', '双侧卵巢', '椎间盘', '示', '横断面示',
                                             '并见', '轴位像见', '轴位见', '定位片示', '矢状面于',
                                             '矢状面示', '病灶中央液化呈', '矢状面扫描示', '椎体平面脊髓',
                                             '椎体', '垂体', '腰椎', '颈椎', '胸椎', '扫描序列包括',
                                             '扫描序列', '包括', '序列', '成像', '检查', '技术',
                                             '上', '中', '下', '前', '后', '左', '右', '内', '外',
                                             '近', '远', '浅', '深', '大', '小', '多', '少', '长', '短']
                    
                    # 检查chinese_part是否是常见医学模式的一部分
                    if any(pattern in chinese_part for pattern in common_medical_patterns):
                        is_medical_term = True
                
                # 7. 检查是否是定位片、轴位像等特定描述后的字母
                elif not is_medical_term and re.match(r'^(定位片示|轴位像见|横断面示|轴位见|矢状面于|矢状面示|矢状面扫描示|肝|肺|脾|肾|胰|胆|心|脑|脊髓|骨|关节|软组织|血管|神经|肌肉|韧带|肌腱)$', chinese_part):
                    is_medical_term = True
                
                # 8. 检查是否是类似"示L"、"腰椎T"、"肝S"的情况
                elif not is_medical_term and chinese_part in ['示', '腰椎', '颈椎', '胸椎', '肝', '肺', '脾', '肾', '胰', '胆', '心', '脑', '脊髓', '骨', '关节']:
                    is_medical_term = True
                
                if not is_medical_term:
                    found_typos.append({
                        'word': f'{chinese_part}{letters_part}',
                        'correction': chinese_part,
                        'severity': 'medium',
                        'context': sentence[:50] + '...' if len(sentence) > 50 else sentence,
                        'confidence': 0.8,
                        'type': '中文后误输入字母'
                    })

            # 4.3 检测中文词中间插入的单个或多个英文字母
            # 改进：排除正常的医学表达式
            chinese_inside_letters_pattern = r'([\u4e00-\u9fff]+)([a-zA-Z]+)([\u4e00-\u9fff]+)'
            chinese_inside_letters_matches = re.findall(chinese_inside_letters_pattern, sentence)
            for chinese_part1, letters_part, chinese_part2 in chinese_inside_letters_matches:
                # 字母本身是已知术语（「肝TACE术后」）→ 正常写法
                if self._is_known_medical_term(letters_part):
                    continue
                # 同上：已由 4.1 报过的乱码不重复报
                if (len(letters_part) >= 3
                        and not self._is_known_medical_term(letters_part)
                        and self._looks_like_typo_noise(letters_part)):
                    continue
                # 检查是否是正常的医学表达式
                is_medical_term = False
                
                # 排除常见的医学表达式，如"结合CTA示"、"脑CT显示"、"颌面部CT扫描"、"肝ca复查"等
                if letters_part.upper() in ['CT', 'MRI', 'DR', 'US', 'ECG', 'PET', 'SPECT', 'DWI', 'ADC', 'DTPA', 'MRCP', 'MRU', 'MRA', 'CTA', 'DSA', 'XRAY', 'CR', 'ROI', 'FOV', 'SUV', 'TR', 'TE', 'TI', 'FA', 'ETL', 'NEX', 'FSE', 'GRE', 'SE', 'EPI', 'STIR', 'MPR', 'MIP', 'AVM', 'SET', 'Cine', 'CineMRI', 'MRCCP', 'CA', 'ICU', 'FS', 'FS-PD', 'FS-T2', 'PD', 'PDWI', 'PDFS', 'VR', 'CTA', 'IM', 'DAS', 'PICC']:
                    is_medical_term = True
                # 排除类似"结合CTA示"、"脑CT显示"、"肝ca复查"的情况
                elif (chinese_part1.endswith(('结合', '脑', '颌面部', '符合', '肝', '肺')) and 
                      letters_part.upper() in ['CTA', 'CT', 'MRI', 'DR', 'US', 'ECG', 'PET', 'SPECT', 'DWI', 'ADC', 'DTPA', 'MRCP', 'MRU', 'MRA', 'CTA', 'DSA', 'XRAY', 'CR', 'AVM', 'Grave', 'CA', 'ICU', 'FS', 'FS-PD', 'FS-T2', 'PD', 'PDWI', 'PDFS', 'DAS']):
                    is_medical_term = True
                # 排除类似"建议MRI进一步检查"、"腰椎T1高信号"的情况
                elif (chinese_part1 in ['建议', '腰椎', '颈椎', '胸椎'] and 
                      letters_part.upper() in ['MRI', 'CT', 'DR', 'US', 'ECG', 'PET', 'SPECT', 'DWI', 'ADC', 'DTPA', 'MRCP', 'MRU', 'MRA', 'CTA', 'DSA', 'XRAY', 'CR', 'CA', 'ICU', 'FS', 'FS-PD', 'FS-T2', 'PD', 'PDWI', 'PDFS', 'T1', 'T2', 'T1W', 'T2W', 'T1WI', 'T2WI', 'DAS']):
                    is_medical_term = True
                
                if not is_medical_term:
                    found_typos.append({
                        'word': f'{chinese_part1}{letters_part}{chinese_part2}',
                        'correction': f'{chinese_part1}{chinese_part2}',
                        'severity': 'medium',
                        'context': sentence[:50] + '...' if len(sentence) > 50 else sentence,
                        'confidence': 0.8,
                        'type': '中文中间误输入字母'
                    })

        # 去重
        seen = set()
        unique_typos = []
        for typo in found_typos:
            try:
                # 确保word和context都存在且不为空
                word = typo.get('word', '')
                context = typo.get('context', '')
                key = f"{word}:{context[:30]}"
                if key not in seen:
                    seen.add(key)
                    unique_typos.append(typo)
            except Exception as e:
                # 处理去重过程中的异常，避免闪退
                print(f"去重时发生异常: {e}")
                continue

        return unique_typos
