#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能文本检查器 - 逻辑检查模块
"""

import re
from typing import List, Dict


class LogicChecker:
    """逻辑检查模块"""

    def _check_exception_rules(self, text: str) -> Dict:
        """检查例外规则"""
        for rule in self.exception_rules:
            if re.search(rule['pattern'], text):
                return {
                    'is_normal': True,
                    'explanation': rule['explanation'],
                    'type': rule['type']
                }

        return {'is_normal': False, 'explanation': ''}

    def _smart_check_terminology(self, text: str, report_type: str, check_method: str) -> List[Dict]:
        """智能术语匹配检查"""
        issues = []

        # 确定报告类型上下文
        context_type = self._determine_report_context(text, report_type, check_method)

        if context_type in self.contextual_expressions:
            valid_terms = self.contextual_expressions[context_type]['valid']
            contextual_valid = self.contextual_expressions[context_type].get('contextual_valid', [])

            # 检查无效术语 - 但排除建议检查的情况
            invalid_checks = {
                'CT': ['信号', 'T1WI', 'T2WI'],
                'MRI': ['密度', 'CT值'],
                'DR': ['信号', 'T1WI', 'T2WI'],
            }

            if context_type in invalid_checks:
                for term in invalid_checks[context_type]:
                    if term in text:
                        # 检查是否有合理的上下文解释
                        if not self._has_contextual_explanation(term, text, context_type):
                            # 关键修改：如果是在"建议检查"的上下文中，不报错
                            if self._is_recommendation_context(text, term):
                                continue

                            # 如果是信号和密度同时出现，会有专门的检查，这里跳过
                            if (term == '信号' and '密度' in text) or (term == '密度' and '信号' in text):
                                continue

                            issues.append({
                                'type': '术语不匹配',
                                'description': f'{context_type}报告中通常不使用"{term}"',
                                'suggestion': f'考虑使用{valid_terms[:3]}等术语',
                                'confidence': 0.8,
                                'keywords': [term]
                            })

        return issues

    def _smart_check_gender_organs(self, text: str, gender: str) -> List[Dict]:
        """智能性别器官检查"""
        issues = []

        if gender == '男':
            # 男性报告中提及子宫、附件等
            female_organs = ['子宫', '卵巢', '输卵管', '阴道', '宫颈']
            for organ in female_organs:
                if organ in text:
                    issues.append({
                        'type': '性别器官逻辑错误',
                        'description': f'男性报告中提及"{organ}"',
                        'suggestion': '请检查患者性别与报告内容是否匹配',
                        'confidence': 0.9,
                        'keywords': [organ]
                    })

            # 特殊处理"附件"：需要区分"椎体附件"（正常）和"子宫附件"（异常）
            if '附件' in text:
                # 检查是否是正常的椎体附件描述
                if not self._is_normal_spine_appendage_context(text):
                    issues.append({
                        'type': '性别器官逻辑错误',
                        'description': '男性报告中提及"附件"',
                        'suggestion': '请检查患者性别与报告内容是否匹配',
                        'confidence': 0.9,
                        'keywords': ['附件']
                    })

        elif gender == '女':
            # 女性报告中提及前列腺、精囊等
            male_organs = ['前列腺', '精囊', '精索', '睾丸', '阴茎']
            for organ in male_organs:
                if organ in text:
                    issues.append({
                        'type': '性别器官逻辑错误',
                        'description': f'女性报告中提及"{organ}"',
                        'suggestion': '请检查患者性别与报告内容是否匹配',
                        'confidence': 0.9,
                        'keywords': [organ]
                    })

        return issues

    def _smart_check_left_right_consistency(self, text: str) -> List[Dict]:
        """智能左右一致性检查 - 只在描述不一致时提醒"""
        issues = []

        # 提取所有部分
        sections = self._extract_report_sections(text)
        desc_text = sections['描述']

        # 提取所有左侧描述和右侧描述
        left_descriptions = self._extract_side_descriptions(desc_text, 'left')
        right_descriptions = self._extract_side_descriptions(desc_text, 'right')

        # 只有在左右侧都有描述时才检查一致性
        if left_descriptions and right_descriptions:
            # 检查左右侧描述是否明显不一致
            if self._is_side_description_inconsistent(left_descriptions, right_descriptions):
                issues.append({
                    'type': '左右描述不一致',
                    'description': '左右侧描述存在明显不一致',
                    'suggestion': '请检查左右侧描述是否准确对应',
                    'confidence': 0.7,
                    'keywords': ['左', '右']
                })

        return issues

    def _check_contradictions(self, text: str) -> List[Dict]:
        """检查矛盾表达 - 排除正常医学描述"""
        issues = []

        # 首先检查是否是正常的医学描述
        for normal_pattern in self.normal_medical_patterns:
            if re.search(normal_pattern, text):
                # 如果是正常医学描述，不进行矛盾检查
                return issues

        # 分句检查，避免跨句误判
        sentences = re.split(r'[。！？；\n]', text)

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 3:
                continue

            for pattern, description in self.contradiction_patterns:
                matches = re.findall(pattern, sentence)
                if matches:
                    # 进一步验证是否是真正的矛盾
                    if not self._is_real_contradiction(sentence, pattern):
                        continue

                    # 提取具体的矛盾词语
                    for match in matches:
                        if isinstance(match, tuple):
                            # 如果匹配返回的是元组，取第一个非空元素
                            actual_text = next((m for m in match if m), match[0] if match else "")
                        else:
                            actual_text = match

                        issues.append({
                            'type': '矛盾表达',
                            'description': description,
                            'suggestion': '请检查表达是否准确',
                            'confidence': 0.9,
                            'keywords': [actual_text] if actual_text else []
                        })

        return issues

    def _is_normal_full_description(self, text: str) -> bool:
        """检查整个文本是否是正常的医学描述"""
        # 完整的正常描述模式
        normal_full_descriptions = [
            "脊髓圆锥和马尾形态信号正常，髓内未见异常信号",
            "桡侧、尺侧副韧带走行正常，信号未见明显异常",
            "胆囊壁稍增厚，腔内液区清晰。胰腺形态、大小可，胰头部密度不均匀，边缘稍模糊",
            "肝脏形态、大小正常，表面光滑，边缘锐利，肝实质回声均匀",
            "双肾形态、大小正常，包膜光滑，肾实质回声均匀，集合系统未见分离",
        ]

        for desc in normal_full_descriptions:
            if desc in text:
                return True

        return False

    def _is_real_contradiction(self, sentence: str, pattern: str) -> bool:
        """判断是否是真正的矛盾（而不是正常医学描述）"""
        # 正常的医学连续表达不应被误判
        normal_expressions = [
            "形态信号正常，未见异常信号",
            "走行正常，信号未见异常",
            "走行正常，信号未见明显异常",
            "密度不均匀，边缘模糊",
            "密度均匀，信号均匀",
            "边界清楚，形态规则",
            "形态规则，边界清楚",
            "走行自然，边界清楚",
            "信号均匀，未见异常",
            "密度均匀，未见异常",
            "形态可，信号正常",
        ]

        # 如果整个句子匹配任何正常表达，不是矛盾
        for normal_expr in normal_expressions:
            if normal_expr in sentence:
                return False

        return True

    def _has_reasonable_context(self, organ: str, text: str, gender: str) -> bool:
        """检查是否有合理的上下文"""
        reasonable_patterns = [
            rf'与.*{organ}.*对比',  # 与其他器官对比
            rf'{organ}.*未见',  # 未见该器官（在描述正常解剖时可能提到）
            rf'既往.*{organ}.*史',  # 既往病史中提到
            rf'家族.*{organ}.*史',  # 家族史中提到
        ]

        for pattern in reasonable_patterns:
            if re.search(pattern, text):
                return True

        return False

    def _is_normal_spine_appendage_context(self, text: str) -> bool:
        """检查'附件'是否在正常的椎体附件上下文中"""
        # 正常的椎体附件上下文模式（必须包含椎体相关的关键词）
        spine_appendage_patterns = [
            r'椎体.*附件',  # 椎体附件
            r'附件.*骨质',  # 附件骨质
            r'附件.*骨折',  # 附件骨折
            r'附件.*增生',  # 附件增生
            r'附件.*破坏',  # 附件破坏
            r'附件.*完整',  # 附件完整（需要结合椎体上下文）
            r'腰椎.*附件',  # 腰椎附件
            r'胸椎.*附件',  # 胸椎附件
            r'颈椎.*附件',  # 颈椎附件
        ]

        # 异常的子宫附件上下文模式（应该被排除）
        abnormal_patterns = [
            r'双侧.*附件',  # 双侧附件（通常指子宫附件）
            r'附件区',  # 附件区（通常指子宫附件区）
            r'子宫.*附件',  # 子宫附件
            r'盆腔.*附件',  # 盆腔附件
            r'附件.*囊肿',  # 附件囊肿（妇科）
            r'附件.*肿瘤',  # 附件肿瘤（妇科）
            r'附件.*炎症',  # 附件炎症（妇科）
        ]

        # 首先检查是否是异常的子宫附件上下文
        for pattern in abnormal_patterns:
            if re.search(pattern, text):
                return False

        # 然后检查是否是正常的椎体附件上下文
        for pattern in spine_appendage_patterns:
            if re.search(pattern, text):
                return True

        # 如果都不匹配，默认为异常（保守策略）
        return False

    def _check_signal_density_conflict(self, text: str, check_method: str, report_type: str) -> List[Dict]:
        """检查信号和密度是否在不当的上下文中同时出现"""
        issues = []

        # 只有当文本中同时出现"信号"和"密度"时才检查
        if '信号' in text and '密度' in text:
            # 检查是否是正常的对比或建议
            has_reasonable_context = False

            # 合理的上下文模式
            reasonable_patterns = [
                r'请结合.*CT.*检查',
                r'建议.*CT.*检查',
                r'结合.*CT.*检查',
                r'CT.*检查',
                r'对比.*CT',
                r'参照.*CT',
            ]

            for pattern in reasonable_patterns:
                if re.search(pattern, text):
                    has_reasonable_context = True
                    break

            if not has_reasonable_context:
                # 确定报告类型上下文
                context_type = self._determine_report_context(text, report_type, check_method)

                if context_type == 'MRI':
                    # 查找密度描述的具体位置
                    sentences = re.split(r'[。！？；\n]', text)
                    for sentence in sentences:
                        if '密度' in sentence and '信号' not in sentence:
                            # 检查是否是MRI报告中的密度描述
                            if 'MRI' in check_method.upper() or '磁共振' in check_method:
                                issues.append({
                                    'type': '术语冲突',
                                    'description': 'MRI报告中通常使用"信号"而非"密度"描述',
                                    'suggestion': '建议将"密度"改为"信号"，或明确说明是参考其他检查',
                                    'confidence': 0.8,
                                    'keywords': ['密度', '信号']
                                })
                                break

        return issues

    def _determine_report_context(self, text: str, report_type: str, check_method: str) -> str:
        """确定报告上下文类型"""
        text_upper = text.upper()

        # 先检查MRI，因为MRI报告中可能包含CT术语（如对比CT）
        if 'MR' in text_upper or 'MRI' in text_upper or '磁共振' in text or 'MRI' in report_type or 'MRI' in check_method:
            return 'MRI'
        elif 'CT' in text_upper or 'CT' in report_type or 'CT' in check_method:
            return 'CT'
        elif 'DR' in text_upper or '数字摄影' in text or 'X线' in text:
            return 'DR'
        else:
            # 根据文本内容推断
            if '信号' in text and ('T1WI' in text or 'T2WI' in text or 'DWI' in text or 'ADC' in text):
                return 'MRI'
            elif '密度' in text and '信号' not in text:
                return 'CT'
            elif '信号' in text and '密度' not in text:
                return 'MRI'
            else:
                return '未知'

    def _is_recommendation_context(self, text: str, term: str) -> bool:
        """检查是否在建议检查的上下文中"""
        # 查找"建议"或"结合"等关键词附近的文本
        recommendation_patterns = [
            r'建议.*(?:CT|MRI).*检查',
            r'结合.*(?:CT|MRI).*检查',
            r'请.*结合.*(?:CT|MRI)',
            r'必要时.*(?:CT|MRI).*检查',
        ]

        # 检查整个文本中是否有建议检查的描述
        for pattern in recommendation_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                # 如果是在建议检查的语境中，允许出现不同检查类型的术语
                return True

        # 检查是否是描述既往检查或对比
        comparison_patterns = [
            r'既往.*(?:CT|MRI).*显示',
            r'对比.*(?:CT|MRI).*表现',
            r'与.*(?:CT|MRI).*对比',
            r'参照.*(?:CT|MRI).*检查',
        ]

        for pattern in comparison_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True

        return False

    def _has_contextual_explanation(self, term: str, text: str, context_type: str) -> bool:
        """检查是否有上下文解释"""
        # 在某些上下文中，某些术语可能是合理的
        contextual_patterns = {
            '信号': [
                r'CT.*血管.*信号',  # CTA中的血流信号
                r'CT.*造影.*信号',  # 增强CT中的信号
                r'建议.*MRI.*检查',  # 建议MRI检查时提到信号
                r'结合.*MRI.*检查',  # 结合MRI检查时提到信号
            ],
            '密度': [
                r'MRI.*CT.*对比',  # MRI与CT对比时提到密度
                r'既往.*CT.*显示',  # 提及既往CT结果
                r'建议.*CT.*检查',  # 建议CT检查时提到密度
                r'结合.*CT.*检查',  # 结合CT检查时提到密度
            ]
        }

        if term in contextual_patterns:
            for pattern in contextual_patterns[term]:
                if re.search(pattern, text, re.IGNORECASE):
                    return True

        # 检查是否在建议检查的上下文中
        if self._is_recommendation_context(text, term):
            return True

        return False

    def _filter_false_contradictions(self, logic_issues: List[Dict], text: str) -> List[Dict]:
        """过滤掉正常医学描述中的假矛盾"""
        filtered_issues = []

        for issue in logic_issues:
            if issue.get('type') == '矛盾表达':
                # 检查是否是正常的医学描述
                description = issue.get('description', '')
                keywords = issue.get('keywords', [])

                # 如果是以下情况，可能是假矛盾
                is_false_positive = False

                # 检查关键词是否在正常医学描述中
                for keyword in keywords:
                    if keyword in ['信号', '密度', '形态', '走行', '边缘', '壁']:
                        # 检查上下文是否是正常的医学描述
                        if self._is_normal_medical_context(text, keyword):
                            is_false_positive = True
                            break

                # 如果不是假矛盾，保留
                if not is_false_positive:
                    filtered_issues.append(issue)
            else:
                # 非矛盾问题直接保留
                filtered_issues.append(issue)

        return filtered_issues

    def _is_normal_medical_context(self, text: str, keyword: str) -> bool:
        """检查关键词是否在正常的医学上下文中"""
        # 正常的医学描述模式
        normal_patterns = {
            '信号': [
                r'形态信号正常',
                r'信号未见.*异常',
                r'信号均匀',
                r'信号.*正常',
                r'T1WI.*T2WI',
                r'高信号.*低信号',
            ],
            '密度': [
                r'密度均匀',
                r'密度未见.*异常',
                r'密度.*正常',
                r'高密度.*低密度',
                r'CT值.*正常',
            ],
            '形态': [
                r'形态正常',
                r'形态规则',
                r'形态可',
                r'形态大小',
            ],
            '走行': [
                r'走行正常',
                r'走行自然',
                r'走行.*清晰',
            ],
            '边缘': [
                r'边缘清楚',
                r'边缘清晰',
                r'边缘稍模糊',  # 这是正常的
                r'边缘规则',
            ],
            '壁': [
                r'壁光滑',
                r'壁稍增厚',  # 这是正常的
                r'壁不厚',
            ]
        }

        if keyword in normal_patterns:
            for pattern in normal_patterns[keyword]:
                if re.search(pattern, text):
                    return True

        return False

    def check_semantic_issues(self, text: str) -> List[Dict]:
        """检查语义问题"""
        issues = []

        # 分句检查
        sentences = re.split(r'[。！？；\n]', text)

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 3:
                continue

            # 检查语义合理性
            semantic_issue = self._check_sentence_semantics(sentence)
            if semantic_issue:
                issues.append(semantic_issue)

        return issues

    def _check_units(self, text: str) -> List[str]:
        warnings = []
        cm_matches = re.findall(r'\b\d+(?:\.\d+)?\s*(?:cm|CM)\b', text)
        mm_upper_matches = re.findall(r'(?<![a-zA-Z])\d+(?:\.\d+)?\s*MM(?![a-zA-Z])', text)
        if cm_matches:
            warnings.append("检测到厘米单位(cm/CM)，默认单位为mm，请确认并统一")
        if mm_upper_matches:
            warnings.append("检测到单位为大写MM，建议统一为小写mm")
        return warnings

    def _check_sentence_semantics(self, sentence: str) -> Dict:
        """检查句子语义合理性"""
        # 检查明显的语义问题
        semantic_problems = [
            (r"明显正常", "明显正常不太符合常规表达，建议使用大致正常或基本正常"),
            (r"显著正常", "显著正常不太符合常规表达"),
            (r"非常正常", "非常正常不太符合常规表达"),
            (r"完全正常", "完全正常虽然可用，但医学报告中较少使用"),
            (r"绝对正常", "绝对正常不符合医学谨慎原则"),
            (r"肯定正常", "肯定正常过于绝对"),
        ]

        for pattern, suggestion in semantic_problems:
            if re.search(pattern, sentence):
                return {
                    'type': '语义表达问题',
                    'description': f'"{pattern}"可能不符合医学报告常规',
                    'suggestion': suggestion,
                    'confidence': 0.7,
                    'keywords': [pattern.strip('()')]
                }

        return None

    def check_context_consistency(self, text: str, report_type: str) -> List[Dict]:
        """检查上下文一致性 - 降低敏感度"""
        issues = []

        # 检查前后文的一致性
        sections = self._extract_report_sections(text)

        # 如果某些部分缺失，不进行严格检查
        missing_sections = [key for key, value in sections.items() if not value]
        if len(missing_sections) > 1:  # 如果缺失超过1个部分，跳过检查
            return issues

        if sections['方法'] and sections['提示']:
            # 检查方法与提示的一致性 - 降低要求
            method_keywords = self._extract_keywords(sections['方法'])
            prompt_keywords = self._extract_keywords(sections['提示'])

            # 检查主要发现是否在提示中体现 - 只检查明显的主要发现
            major_findings = self._extract_major_findings(sections['描述'])

            # 只检查长度大于2且明显异常的关键词
            significant_findings = [
                finding for finding in major_findings
                if len(finding) > 2 and any(keyword in finding for keyword in ['增大', '肿块', '结节', '积液', '炎症'])
            ]

            for finding in significant_findings:
                if finding not in prompt_keywords:
                    # 检查是否有类似的词（部分匹配）
                    similar_found = any(finding in kw or kw in finding for kw in prompt_keywords)
                    if not similar_found:
                        # 只有当置信度足够高时才标记
                        confidence = self._calculate_context_confidence(finding, sections['描述'], sections['提示'])
                        if confidence > 0.7:  # 提高置信度阈值
                            issues.append({
                                'type': '上下文一致性',
                                'description': f'描述中提到"{finding}"，但提示中未体现',
                                'suggestion': '建议在提示中体现主要发现',
                                'confidence': confidence,
                                'keywords': [finding]
                            })

        return issues

    def _calculate_context_confidence(self, finding: str, description: str, prompt: str) -> float:
        """计算上下文一致性的置信度"""
        confidence = 0.5  # 基础置信度

        # 检查发现的重要程度
        important_keywords = ['明显', '显著', '重要', '主要', '主要发现', '关键发现']
        if any(keyword in description for keyword in important_keywords):
            confidence += 0.2

        # 检查发现的位置（是否在段落开头）
        if description.find(finding) < 100:  # 在前100个字符内
            confidence += 0.1

        # 检查提示中是否有相关的描述
        related_keywords = self._get_related_keywords(finding)
        if any(keyword in prompt for keyword in related_keywords):
            confidence -= 0.3  # 如果有相关词，降低置信度

        return min(1.0, max(0.0, confidence))

    def _get_related_keywords(self, finding: str) -> List[str]:
        """获取相关关键词"""
        # 定义常见发现的相关词
        related_map = {
            '增大': ['扩大', '增厚', '肥厚', '肿胀'],
            '肿块': ['占位', '肿物', '包块', '结节'],
            '结节': ['肿物', '肿块', '小结节', '微小'],
            '积液': ['液性', '液区', '液暗', '积水'],
            '炎症': ['炎性', '充血', '水肿', '渗出'],
            '增生': ['增殖', '增厚', '肥大'],
            '萎缩': ['缩小', '变薄', '减少'],
            '硬化': ['钙化', '骨化', '纤维化'],
        }

        for key, related in related_map.items():
            if key in finding:
                return related

        return []

    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词"""
        # 简单实现：提取2-4个字符的中文词
        keywords = re.findall(r'[\u4e00-\u9fff]{2,4}', text)
        return list(set(keywords))

    def _extract_major_findings(self, text: str) -> List[str]:
        """提取主要发现"""
        findings = []

        # 查找描述中的异常发现
        abnormal_patterns = [
            r'([\u4e00-\u9fff]+)增大',
            r'([\u4e00-\u9fff]+)扩大',
            r'([\u4e00-\u9fff]+)增粗',
            r'([\u4e00-\u9fff]+)结节',
            r'([\u4e00-\u9fff]+)肿块',
            r'([\u4e00-\u9fff]+)占位',
            r'([\u4e00-\u9fff]+)积液',
            r'([\u4e00-\u9fff]+)炎症',
        ]

        for pattern in abnormal_patterns:
            matches = re.findall(pattern, text)
            findings.extend(matches)

        return list(set(findings))

    def generate_intelligent_suggestions(self, results: Dict, text: str) -> List[str]:
        """生成智能建议"""
        suggestions = []
        return suggestions
