#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能文本检查器 - 左右检测模块
"""

import re
from typing import List, Dict
from collections import Counter


class LeftRightChecker:
    """左右检测模块"""

    def _get_extended_left_patterns(self) -> List[str]:
        """获取扩展的左侧词汇模式"""
        left_patterns = ['左', '左侧', '左边']
        left_body_parts = [
            '左肺', '左肩', '左膝', '左踝', '左腕', '左肘', '左髋', '左足', '左手', '左臂', '左腿',
            '左胸', '左腹', '左肾', '左肝', '左心', '左眼', '左耳', '左鼻', '左面', '左唇', '左舌',
            '左甲状腺', '左乳腺', '左卵巢', '左睾丸', '左肢体', '左半', '左叶', '左房', '左室', '左脑血管',
            '左半', '左侧', '左旁', '左基底', '左顶叶', '左枕叶', '左颞叶', '左额叶', '左小脑', '左脑干',
            '左肾上腺', '左输尿管', '左股骨', '左胫骨', '左腓骨', '左尺骨', '左桡骨', '左锁骨', '左肩胛骨',
            '左肋', '左膈', '左主支气管', '左肺动脉', '左肺静脉', '左上腔静脉'
        ]
        left_direction = ['左方', '左部', '左区', '左段', '左缘', '左壁', '左腔', '左窦', '左窝', '左孔',
                         '左裂', '左带', '左索', '左束', '左脚', '左板', '左翼', '左帽']
        return left_patterns + left_body_parts + left_direction

    def _get_extended_right_patterns(self) -> List[str]:
        """获取扩展的右侧词汇模式"""
        right_patterns = ['右', '右侧', '右边']
        right_body_parts = [
            '右肺', '右肩', '右膝', '右踝', '右腕', '右肘', '右髋', '右足', '右手', '右臂', '右腿',
            '右胸', '右腹', '右肾', '右肝', '右心', '右眼', '右耳', '右鼻', '右面', '右唇', '右舌',
            '右甲状腺', '右乳腺', '右卵巢', '右睾丸', '右肢体', '右半', '右叶', '右房', '右室', '右脑血管',
            '右半', '右侧', '右旁', '右基底', '右顶叶', '右枕叶', '右颞叶', '右额叶', '右小脑', '右脑干',
            '右肾上腺', '右输尿管', '右股骨', '右胫骨', '右腓骨', '右尺骨', '右桡骨', '右锁骨', '右肩胛骨',
            '右肋', '右膈', '右主支气管', '右肺动脉', '右肺静脉', '右上腔静脉'
        ]
        right_direction = ['右方', '右部', '右区', '右段', '右缘', '右壁', '右腔', '右窦', '右窝', '右孔',
                          '右裂', '右带', '右索', '右束', '右脚', '右板', '右翼', '右帽']
        return right_patterns + right_body_parts + right_direction

    def _extract_side_descriptions(self, text: str, side: str) -> List[str]:
        abnormal_keywords = ['增大', '缩小', '增厚', '变薄', '异常', '病变', '肿块', '结节', '积液',
                            '高信号', '低信号', '等信号', '混杂信号', '低密度', '高密度', '等密度',
                            '模糊', '不清', '不规则', '破坏', '侵蚀', '变形', '移位', '突出', '膨出',
                            '变窄', '增生', '影', '密度不均', '信号不均', '占位', '损伤', '骨折', '退变']
        normal_keywords = ['正常', '未见异常', '清晰', '均匀', '规则', '对称', '无异常',
                          '信号未见异常', '密度均匀', '形态规则', '边缘清晰', '走行正常', '大致正常']
        left_patterns = self._get_extended_left_patterns()
        right_patterns = self._get_extended_right_patterns()
        both_patterns = ['双', '双侧', '双边', '两', '两侧']

        descriptions = []
        sentences = re.split(r'[，。！？；\n]', text)
        for sentence in sentences:
            s = sentence.strip()
            if not s:
                continue
            if any(bp in s for bp in both_patterns):
                continue
            has_left = any(lp in s for lp in left_patterns)
            has_right = any(rp in s for rp in right_patterns)
            if side == 'left' and has_left and not has_right:
                if any(kw in s for kw in abnormal_keywords) or not any(kw in s for kw in normal_keywords):
                    descriptions.append(s)
            elif side == 'right' and has_right and not has_left:
                if any(kw in s for kw in abnormal_keywords) or not any(kw in s for kw in normal_keywords):
                    descriptions.append(s)
        
        return descriptions

    def _check_organ_side_logic(self, text: str) -> List[Dict]:
        """检查器官与侧别的解剖学逻辑错误"""
        issues = []
        # 定义器官及其通常所在的侧别
        # 肝脏主部在右（左叶跨过中线），脾脏在左，胆囊在右，阑尾在右（通常），胰头在右，胰尾在左
        organ_side_rules = [
            {'organ': '脾脏', 'correct_side': '左', 'wrong_side': '右', 'confidence': 0.8},
            {'organ': '胆囊', 'correct_side': '右', 'wrong_side': '左', 'confidence': 0.8},
            {'organ': '阑尾', 'correct_side': '右', 'wrong_side': '左', 'confidence': 0.7},
            {'organ': '胃', 'correct_side': '左', 'wrong_side': '右', 'confidence': 0.6},
            {'organ': '乙状结肠', 'correct_side': '左', 'wrong_side': '右', 'confidence': 0.7},
            {'organ': '升结肠', 'correct_side': '右', 'wrong_side': '左', 'confidence': 0.8},
            {'organ': '降结肠', 'correct_side': '左', 'wrong_side': '右', 'confidence': 0.8},
        ]
        
        for rule in organ_side_rules:
            pattern = rf"{rule['wrong_side']}[侧边]?{rule['organ']}"
            if re.search(pattern, text):
                issues.append({
                    'type': '解剖侧位错误',
                    'description': f"通常情况下{rule['organ']}位于{rule['correct_side']}侧，但报告中提及{rule['wrong_side']}侧{rule['organ']}",
                    'suggestion': f"请核实{rule['organ']}的侧位描述是否准确",
                    'confidence': rule['confidence'],
                    'keywords': [rule['wrong_side'], rule['organ']]
                })
        
        return issues

    def _is_side_description_inconsistent(self, left_desc: List[str], right_desc: List[str]) -> bool:
        abnormal_keywords = ['增大', '缩小', '增厚', '变薄', '异常', '病变', '肿块', '结节', '积液',
                            '高信号', '低信号', '等信号', '混杂信号', '低密度', '高密度', '等密度',
                            '模糊', '不清', '不规则', '破坏', '侵蚀', '变形', '移位', '突出', '膨出',
                            '变窄', '增生', '影', '密度不均', '信号不均', '占位', '损伤', '骨折', '退变']
        normal_keywords = ['正常', '未见异常', '清晰', '均匀', '规则', '对称', '无异常',
                          '信号未见异常', '密度均匀', '形态规则', '边缘清晰', '走行正常', '大致正常']

        if not left_desc or not right_desc:
            return False

        def count_flags(descriptions, keywords):
            count = 0
            for desc in descriptions:
                if any(k in desc for k in keywords):
                    count += 1
            return count

        left_abn = count_flags(left_desc, abnormal_keywords)
        right_abn = count_flags(right_desc, abnormal_keywords)
        left_norm = count_flags(left_desc, normal_keywords)
        right_norm = count_flags(right_desc, normal_keywords)

        if (left_abn >= 2 and right_abn == 0 and right_norm >= 1) or (right_abn >= 2 and left_abn == 0 and left_norm >= 1):
            return True
        return False

    def _smart_check_left_right_consistency(self, text: str) -> List[Dict]:
        """智能检查内容中的左右侧一致性"""
        issues = []
        if ('左' not in text) or ('右' not in text):
            return issues
        left_desc = self._extract_side_descriptions(text, 'left')
        right_desc = self._extract_side_descriptions(text, 'right')
        
        if self._is_side_description_inconsistent(left_desc, right_desc):
            issues.append({
                'type': '左右侧描述矛盾',
                'description': '左右侧描述存在明显不一致（如一侧正常而另一侧异常，或存在相反描述）',
                'suggestion': '请核实左右侧的病情描述是否准确对应',
                'confidence': 0.75,
                'keywords': ['左', '右']
            })
            
        return issues

    def _check_side_title_consistency(self, text: str) -> List[Dict]:
        issues = []

        # 提取器官解剖侧位错误
        issues.extend(self._check_organ_side_logic(text))

        # 提取所有部分
        sections = self._extract_report_sections(text)
        title_text = self._extract_report_title(text)

        # 1. 检查描述与提示中的侧别一致性（新增）
        issues.extend(self._check_desc_prompt_side_consistency(sections))

        # 2. 三区域联合侧别一致性检查
        issues.extend(self._check_combined_side_consistency(text))

        # 从标题中提取主要侧别
        title_sides = self._extract_all_sides_from_text(title_text)
        method_sides = self._extract_all_sides_from_text(sections['方法'])
        desc_sides = self._extract_all_sides_from_text(sections['描述'])
        prompt_sides = self._extract_all_sides_from_text(sections['提示'])

        # 合并所有侧别信息（标题和方法通常指定了检查范围）
        # 优先级：标题 > 方法
        all_primary_sides = title_sides if title_sides else method_sides

        if all_primary_sides:
            # 统计出现次数
            side_counter = Counter(all_primary_sides)
            primary_side = side_counter.most_common(1)[0][0]
            
            # 如果是单侧检查（如“左膝”），则内容中不应出现相反的侧别
            # 但要注意“双侧”检查不在此限
            if '双' not in title_text and '两' not in title_text:
                opposite_side = '右' if primary_side == '左' else '左'
                
                # 在描述中寻找相反侧别，但要排除“双侧对称”等合理表达
                if opposite_side in desc_sides:
                    # 检查是否在合理表达中
                    has_conflict = True
                    for sentence in re.split(r'[，。！？；\n]', sections['描述']):
                        if opposite_side in sentence:
                            # 排除“双侧”、“对侧”、“另一侧”等上下文
                            if any(kw in sentence for kw in ['双侧', '对侧', '另一侧', '比较']):
                                has_conflict = False
                                break
                    
                    if has_conflict:
                        issues.append({
                            'type': '左右侧矛盾',
                            'description': f'标题/方法指定检查{primary_side}侧，但描述中出现{opposite_side}侧描述',
                            'suggestion': f'请统一使用{primary_side}侧描述或修正标题',
                            'confidence': 0.8,
                            'keywords': [primary_side, opposite_side]
                        })

                if opposite_side in prompt_sides:
                    issues.append({
                        'type': '左右侧矛盾',
                        'description': f'标题/方法指定检查{primary_side}侧，但提示中出现{opposite_side}侧描述',
                        'suggestion': f'请统一使用{primary_side}侧描述或修正标题',
                        'confidence': 0.9,
                        'keywords': [primary_side, opposite_side]
                    })
        
        method_text = sections.get('方法', '') or ''
        method_side = 'neutral'
        if '双' in method_text or '两' in method_text:
            method_side = 'both'
        elif '左' in method_text:
            method_side = '左'
        elif '右' in method_text:
            method_side = '右'

        if method_side in ['左', '右'] and desc_sides and ('左' in desc_sides and '右' in desc_sides):
            left_desc_count = desc_sides.count('左')
            right_desc_count = desc_sides.count('右')
            opposite_target = '右' if method_side == '左' else '左'
            if (opposite_target == '右' and right_desc_count >= 3 and left_desc_count <= 1) or \
               (opposite_target == '左' and left_desc_count >= 3 and right_desc_count <= 1):
                issues.append({
                    'type': '左右侧描述不平衡',
                    'description': f'检查方法指定{method_side}侧，但描述中{opposite_target}侧出现次数显著更多',
                    'suggestion': '请核实检查侧别与描述是否一致',
                    'confidence': 0.7,
                    'keywords': ['左', '右']
                })

        return issues

    def _check_combined_side_consistency(self, text: str) -> List[Dict]:
        issues = []
        sections = self._extract_report_sections(text)
        method = sections.get('方法', '') or ''
        desc = sections.get('描述', '') or ''
        prompt = sections.get('提示', '') or ''

        def has_both_sides(s: str) -> bool:
            return ('双' in s) or ('两' in s)

        method_side = 'neutral'
        if has_both_sides(method):
            method_side = 'both'
        else:
            if '左' in method:
                method_side = '左'
            elif '右' in method:
                method_side = '右'

        abnormal_keywords = ['增大', '缩小', '增厚', '变薄', '异常', '病变', '肿块', '结节', '积液',
                             '高信号', '低信号', '等信号', '混杂信号', '低密度', '高密度', '等密度',
                             '模糊', '不清', '不规则', '破坏', '侵蚀', '变形', '移位', '突出', '膨出',
                             '变窄', '增生', '影', '密度不均', '信号不均', '占位', '损伤', '骨折', '退变']
        normal_exceptions = ['双侧', '对侧', '另一侧', '比较', '对比', '对称', '未见异常', '正常']

        if method_side in ['左', '右']:
            opposite = '右' if method_side == '左' else '左'
            for part_text, part_name in [(desc, '描述'), (prompt, '提示')]:
                for sentence in re.split(r'[，。！？；\n]', part_text):
                    s = sentence.strip()
                    if not s:
                        continue
                    if opposite in s and any(kw in s for kw in abnormal_keywords) and not any(exc in s for exc in normal_exceptions):
                        issues.append({
                            'type': '左右侧矛盾',
                            'description': f'检查方法指定{method_side}侧，但{part_name}中出现{opposite}侧异常描述',
                            'suggestion': f'请统一使用{method_side}侧或修正检查方法',
                            'confidence': 0.9,
                            'keywords': [method_side, opposite]
                        })
                        break
            return issues

        if method_side == 'both':
            return issues

        desc_sides = self._extract_all_sides_from_text(desc)
        prompt_sides = self._extract_all_sides_from_text(prompt)

        def dominant_side(sides: List[str]):
            if not sides:
                return None
            c = Counter(sides)
            if c.get('左', 0) > 0 and c.get('右', 0) == 0:
                return '左'
            if c.get('右', 0) > 0 and c.get('左', 0) == 0:
                return '右'
            return None

        d_side = dominant_side(desc_sides)
        p_side = dominant_side(prompt_sides)

        if d_side and p_side and d_side != p_side:
            issues.append({
                'type': '所见与提示侧别不一致',
                'description': f'影像所见为{d_side}侧，影像提示为{p_side}侧',
                'suggestion': '请核对所见与提示的侧别一致性',
                'confidence': 0.85,
                'keywords': [d_side, p_side]
            })

        return issues

    def _check_desc_prompt_side_consistency(self, sections: Dict[str, str]) -> List[Dict]:
        """检查描述中有侧别但提示中遗漏的情况"""
        issues = []
        desc = sections.get('描述', '')
        prompt = sections.get('提示', '')
        
        if not desc or not prompt:
            return issues
            
        # 异常关键词，用于判断描述是否为需要提示的异常
        abnormal_keywords = ['增大', '缩小', '增厚', '变薄', '异常', '病变', '肿块', '结节', '积液',
                            '高信号', '低信号', '等信号', '混杂信号', '低密度', '高密度', '等密度',
                            '模糊', '不清', '不规则', '破坏', '侵蚀', '变形', '移位', '突出', '膨出',
                            '变窄', '增生', '影', '密度不均', '信号不均', '占位', '损伤', '骨折', '退变']

        # 分句检查描述
        sentences = re.split(r'[，。！？；\n]', desc)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
                
            # 检查是否有侧别
            side = None
            if '左' in sentence: side = '左'
            elif '右' in sentence: side = '右'
            
            if side:
                # 检查是否包含异常词
                has_abnormal = any(kw in sentence for kw in abnormal_keywords)
                # 排除正常的描述（如“左肾未见异常”）
                is_normal = any(kw in sentence for kw in ['未见异常', '正常', '清晰', '均匀'])
                
                if has_abnormal and not is_normal:
                    # 这是一个应该在提示中出现的异常描述，检查提示中是否有对应的侧别
                    if side not in prompt:
                        # 进一步确认提示中是否提到了该异常，如果提到了但没带侧别，则是遗漏
                        # 这里简单处理：如果提示中完全没有该侧别，则提示可能遗漏
                        issues.append({
                            'type': '提示侧别遗漏',
                            'description': f'描述中提及{side}侧异常，但提示（结论）中未标注侧别',
                            'suggestion': f'建议在提示中明确标注“{side}侧”',
                            'confidence': 0.7,
                            'keywords': [side]
                        })
                        # 找到一个侧别遗漏后就跳过该侧的其他句子，避免重复提醒
                        break
        
        return issues

    def _extract_report_title(self, text: str) -> str:
        """提取报告标题/方法行"""
        # 查找类似"【检查方法】"或开头部分
        lines = text.split('\n')
        title_keywords = ['检查方法', '检查名称', '检查部位', '检查项目', '检查技术']
        for line in lines:
            if any(kw in line for kw in title_keywords):
                return line
        return lines[0] if lines else ""

    def _extract_all_sides_from_text(self, text: str) -> List[str]:
        """从文本中提取所有侧别信息 - 增强版"""
        sides = []

        left_patterns = ['左', '左侧', '左边']
        right_patterns = ['右', '右侧', '右边']

        left_body_parts = [
            '左肺', '左肩', '左膝', '左踝', '左腕', '左肘', '左髋', '左足', '左手', '左臂', '左腿',
            '左胸', '左腹', '左肾', '左肝', '左心', '左眼', '左耳', '左鼻', '左面', '左唇', '左舌',
            '左甲状腺', '左乳腺', '左卵巢', '左睾丸', '左肢体', '左半', '左叶', '左房', '左室', '左脑血管',
            '左旁', '左基底', '左顶叶', '左枕叶', '左颞叶', '左额叶', '左小脑', '左脑干',
            '左肾上腺', '左输尿管', '左股骨', '左胫骨', '左腓骨', '左尺骨', '左桡骨', '左锁骨', '左肩胛骨',
            '左肋', '左膈', '左主支气管', '左肺动脉', '左肺静脉', '左上腔静脉'
        ]

        right_body_parts = [
            '右肺', '右肩', '右膝', '右踝', '右腕', '右肘', '右髋', '右足', '右手', '右臂', '右腿',
            '右胸', '右腹', '右肾', '右肝', '右心', '右眼', '右耳', '右鼻', '右面', '右唇', '右舌',
            '右甲状腺', '右乳腺', '右卵巢', '右睾丸', '右肢体', '右半', '右叶', '右房', '右室', '右脑血管',
            '右旁', '右基底', '右顶叶', '右枕叶', '右颞叶', '右额叶', '右小脑', '右脑干',
            '右肾上腺', '右输尿管', '右股骨', '右胫骨', '右腓骨', '右尺骨', '右桡骨', '右锁骨', '右肩胛骨',
            '右肋', '右膈', '右主支气管', '右肺动脉', '右肺静脉', '右上腔静脉'
        ]

        left_direction = ['左方', '左部', '左区', '左段', '左缘', '左壁', '左腔', '左窦', '左窝', '左孔',
                         '左裂', '左带', '左索', '左束', '左脚', '左板', '左翼', '左帽']
        right_direction = ['右方', '右部', '右区', '右段', '右缘', '右壁', '右腔', '右窦', '右窝', '右孔',
                          '右裂', '右带', '右索', '右束', '右脚', '右板', '右翼', '右帽']

        all_left_patterns = left_patterns + left_body_parts + left_direction
        all_right_patterns = right_patterns + right_body_parts + right_direction

        all_left_patterns.sort(key=len, reverse=True)
        all_right_patterns.sort(key=len, reverse=True)

        processed = set()

        for pattern in all_left_patterns:
            if pattern in text:
                start = 0
                while True:
                    pos = text.find(pattern, start)
                    if pos == -1:
                        break
                    if pos not in processed:
                        sides.append('左')
                        processed.add(pos)
                    start = pos + 1

        for pattern in all_right_patterns:
            if pattern in text:
                start = 0
                while True:
                    pos = text.find(pattern, start)
                    if pos == -1:
                        break
                    if pos not in processed:
                        sides.append('右')
                        processed.add(pos)
                    start = pos + 1

        return sides
