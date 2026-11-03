#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量级中文语法检测模块
用于检测影像报告中的非医学术语语法错误
"""

import re
from typing import List, Dict


class ChineseGrammarChecker:
    """轻量级中文语法检测器"""
    
    def __init__(self):
        """初始化语法检测器"""
        # 句末多余字符模式
        self.sentence_end_errors = [
            (r'[\u4e00-\u9fff]+的[。！？，]', '的', '句末多余的"的"', 'high'),
            (r'[\u4e00-\u9fff]+了[。！？，]', '了', '句末多余的"了"', 'medium'),
            (r'[\u4e00-\u9fff]+着[。！？，]', '着', '句末多余的"着"', 'medium'),
        ]
        
        # 重复词模式
        self.repetition_patterns = [
            (r'([\u4e00-\u9fff]{2,})(?:[，。、；：]?\s*)(\1)', '重复词语', 'high'),
            (r'([\u4e00-\u9fff]{2,})\1', '连续重复词语', 'high'),
        ]
        
        # 常见语病模式
        self.grammar_errors = [
            # 主谓不搭配
            (r'双肺纹理清晰(的|地)[\u4e00-\u9fff]', '双肺纹理清晰的', '主谓搭配错误，"清晰"后不应加"的"', 'high'),
            (r'心影大小形态正常(的|地)[\u4e00-\u9fff]', '心影大小形态正常的', '主谓搭配错误，"正常"后不应加"的"', 'high'),
            
            # 动宾不搭配
            (r'可见[\u4e00-\u9fff]+显示', '可见...显示', '动宾搭配冗余，"可见"和"显示"重复', 'medium'),
            (r'提示[\u4e00-\u9fff]+诊断', '提示...诊断', '动宾搭配冗余', 'medium'),
            
            # 成分残缺
            (r'发现[\u4e00-\u9fff]+，提示', '发现...，提示', '缺少具体内容', 'medium'),
            
            # 语序不当
            (r'[\u4e00-\u9fff]+增加[\u4e00-\u9fff]+密度', '增加...密度', '语序不当，建议"密度增加"', 'low'),
            
            # 杂糅
            (r'主要原因是[\u4e00-\u9fff]+引起的', '主要原因是...引起的', '句式杂糅，应改为"主要原因是..."或"由...引起的"', 'high'),
            (r'原因是[\u4e00-\u9fff]+造成的', '原因是...造成的', '句式杂糅', 'high'),
            
            # 否定失当
            (r'防止[\u4e00-\u9fff]+不', '防止...不', '否定失当，"防止"后不应加"不"', 'high'),
            (r'避免[\u4e00-\u9fff]+不', '避免...不', '否定失当', 'high'),
        ]
        
        # 影像报告专用语病
        self.radiology_errors = [
            # 术语误用 - CT和MRI术语混用
            (r'CT扫描.*?(T1|T2|FLAIR|DWI|ADC).*?信号', 'CT扫描与MRI术语混用', '术语误用，CT用"密度"，MRI用"信号"', 'high'),
            (r'MRI扫描.*?(密度|CT值)', 'MRI扫描与CT术语混用', '术语误用，MRI用"信号"，CT用"密度"', 'high'),
            
            # 信号密度混用
            (r'密度.*?信号', '密度信号混用', '术语误用，CT用"密度"，MRI用"信号"', 'high'),
            (r'信号.*?密度', '信号密度混用', '术语误用，MRI用"信号"，CT用"密度"', 'high'),
            
            # 检查方法描述不当
            (r'CT扫描[\u4e00-\u9fff]+MRI', 'CT扫描...MRI', '检查方法混淆', 'medium'),
            (r'MRI扫描[\u4e00-\u9fff]+CT', 'MRI扫描...CT', '检查方法混淆', 'medium'),
            
            # 解剖位置描述不当
            (r'左侧[\u4e00-\u9fff]+右侧', '左侧...右侧', '同时提及左右，请确认是否正确', 'low'),
            (r'双侧[\u4e00-\u9fff]+单侧', '双侧...单侧', '矛盾描述', 'medium'),
            
            # 时间描述不当
            (r'较前[\u4e00-\u9fff]+较前', '较前...较前', '重复比较', 'medium'),
        ]
        
        # 标点符号错误（v1.19.1 重写：只报「有举证价值的异常」，不再枚举白名单）
        #
        # 原来的三条规则在真实 PACS 报告上误报率极高（实测见 .ocr_tmp/t_punct.py）：
        #   ① `[，,][。！？]` —— 不同类型标点相邻，几乎全是 OCR 抖动，判不出真错；
        #   ② `[汉字]{5,}[^一长串白名单]` —— 白名单漏了半角 , . ( ) : ; 和省略号 …，
        #      医生习惯用半角标点（OCR 也常把「，」认成「,」），于是满篇「异常标点」；
        #   ③ `[，。！？、；：]{2,}` —— 两个句号就报，OCR 抖一下就是一条告警。
        #
        # 现在的判据：**只有报告正文里根本不该出现的字符才报**。
        self.punctuation_errors = [
            # 控制字符 / Unicode 替换符 / 私用区字符：OCR 失败或编码错误的铁证
            (r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\ue000-\uf8ff]', '异常字符', 'high'),
            # 编程符号混进正文（@ # $ ^ | \ ` 在影像报告里不可能出现）
            (r'[\u4e00-\u9fff][@#$^|\\`]', '异常符号', 'low'),
            # 同类标点连续 3 个以上（`，，` 两个多半是 OCR 抖动，不算错）
            (r'([，、；：])\1{2,}', '重复标点', 'low'),
        ]
    
    def check_grammar(self, text: str) -> List[Dict]:
        """
        检测文本中的语法错误
        
        Args:
            text: 待检测的文本
            
        Returns:
            检测到的语法错误列表
        """
        errors = []
        
        # 1. 检测句末多余字符
        errors.extend(self._check_sentence_end_errors(text))
        
        # 2. 检测重复词语
        errors.extend(self._check_repetition(text))
        
        # 3. 检测常见语病
        errors.extend(self._check_grammar_errors(text))
        
        # 4. 检测影像报告专用语病
        errors.extend(self._check_radiology_errors(text))
        
        # 5. 检测标点符号错误
        errors.extend(self._check_punctuation(text))
        
        # 去重
        seen = set()
        unique_errors = []
        for err in errors:
            key = f"{err['word']}:{err['type']}"
            if key not in seen:
                seen.add(key)
                unique_errors.append(err)
        
        return unique_errors
    
    def _check_sentence_end_errors(self, text: str) -> List[Dict]:
        """检测句末多余字符"""
        errors = []
        
        # 按句子分割
        sentences = re.split(r'[。！？\n]', text)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            # 检测句末是否有多余字符
            for pattern, word, desc, severity in self.sentence_end_errors:
                if re.search(pattern, sentence):
                    errors.append({
                        'word': word,
                        'correction': '',
                        'type': '句末多余字符',
                        'description': desc,
                        'severity': severity,
                        'context': sentence[:50] + '...' if len(sentence) > 50 else sentence
                    })
        
        return errors
    
    def _check_repetition(self, text: str) -> List[Dict]:
        """检测重复词语"""
        errors = []
        
        for pattern, desc, severity in self.repetition_patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0]
                errors.append({
                    'word': match,
                    'correction': match,
                    'type': '重复词语',
                    'description': desc,
                    'severity': severity,
                    'context': self._get_context(text, match)
                })
        
        return errors
    
    def _check_grammar_errors(self, text: str) -> List[Dict]:
        """检测常见语病"""
        errors = []
        
        for pattern, word, desc, severity in self.grammar_errors:
            if re.search(pattern, text):
                errors.append({
                    'word': word,
                    'correction': '',
                    'type': '语病',
                    'description': desc,
                    'severity': severity,
                    'context': self._get_context(text, word)
                })
        
        return errors
    
    def _check_radiology_errors(self, text: str) -> List[Dict]:
        """检测影像报告专用语病"""
        errors = []
        
        for pattern, word, desc, severity in self.radiology_errors:
            if re.search(pattern, text):
                errors.append({
                    'word': word,
                    'correction': '',
                    'type': '影像报告语病',
                    'description': desc,
                    'severity': severity,
                    'context': self._get_context(text, word)
                })
        
        return errors
    
    def _check_punctuation(self, text: str) -> List[Dict]:
        """检测标点符号错误

        用 ``finditer`` 取 ``group(0)`` 而不是 ``findall``：重复标点那条规则带
        捕获组，``findall`` 只会返回组内容（丢掉实际连了几个标点）。
        """
        errors = []

        for pattern, desc, severity in self.punctuation_errors:
            for m in re.finditer(pattern, text):
                word = m.group(0)
                # 给用户看得懂的说明（原来只写「异常标点」，不知道指哪）
                if desc == '异常字符':
                    detail = '出现不可见控制字符/乱码（疑似 OCR 或编码错误，请核对本行原文）'
                elif desc == '异常符号':
                    detail = '混入不该出现在报告里的符号「%s」' % word
                elif desc == '重复标点':
                    detail = '标点连续重复「%s」' % word
                else:
                    detail = desc
                errors.append({
                    'word': word,
                    'correction': '',
                    'type': '标点错误',
                    'description': detail,
                    'severity': severity,
                    'context': self._get_context(text, word)
                })

        return errors
    
    def _get_context(self, text: str, keyword: str) -> str:
        """获取关键词上下文"""
        idx = text.find(keyword)
        if idx == -1:
            return ''
        
        start = max(0, idx - 15)
        end = min(len(text), idx + len(keyword) + 15)
        return text[start:end]


# 简单测试
if __name__ == '__main__':
    checker = ChineseGrammarChecker()
    
    test_texts = [
        "双肺纹理清晰的，心影大小形态正常。",
        "影像所见：双肺纹理清晰，纹理清晰，心影正常。",
        "主要原因是肺炎引起的。",
        "CT扫描显示T2高信号。",
        "防止感染不发生。",
    ]
    
    print("=" * 60)
    print("中文语法检测测试")
    print("=" * 60)
    
    for text in test_texts:
        errors = checker.check_grammar(text)
        print(f"\n测试文本: {text}")
        if errors:
            for err in errors:
                print(f"  ✗ [{err['severity']}] {err['type']}: {err['description']}")
        else:
            print("  ✓ 无语法错误")