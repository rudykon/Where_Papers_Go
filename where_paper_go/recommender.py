#!/usr/bin/env python3
"""按榜单等级和研究主题辅助选择投稿会议/期刊。

本工具只做候选召回与排序，不替代投稿者对最新官网、征稿通知和
投稿要求的最终核验。它仅依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .paths import DATA_DIR, PROJECT_ROOT


ROOT = PROJECT_ROOT
DEFAULT_DATA_DIR = DATA_DIR
API_CACHE_DIR_ENV = "WPG_API_CACHE_DIR"
QUERY_EMBEDDING_CACHE_ENV = "WPG_QUERY_EMBEDDING_CACHE"
LIGHTRAG_EMBEDDING_CACHE_ENV = "WPG_LIGHTRAG_EMBEDDING_CACHE"
DATA_FILES = (
    "ccf_conferences_2026.csv",
    "th_cpl_partition_2019.csv",
    "cas_partition_2025.csv",
    "jcr_partition_2025.csv",
)


def _environment_path(name: str) -> Path | None:
    """Return an optional cache binding without creating or touching it."""

    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


CURATED_SCOPE_FILE = "curated_venue_scopes.tsv"
CURATED_SCOPE_VALID_STATUSES = {"draft", "in_review", "approved", "rejected", "superseded"}
CURATED_SCOPE_ACTIVE_STATUSES = {"approved"}

# A long-lived web worker reuses this immutable in-memory graph.  Cache
# freshness is guarded by cheap file stamps, so source edits still trigger the
# existing digest validation and atomic rebuild path.
_GRAPH_RUNTIME_CACHE: dict[
    tuple[Path, Path], tuple[tuple[tuple[str, int, int], ...], object]
] = {}


def _graph_runtime_stamp(data_dir: Path, graph_path: Path) -> tuple[tuple[str, int, int], ...]:
    from .graph_index import SOURCE_FILE_NAMES, vector_path_for_graph

    paths = [graph_path, vector_path_for_graph(graph_path)]
    paths.extend(data_dir / name for name in SOURCE_FILE_NAMES)
    stamp: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
            stamp.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            stamp.append((str(path.resolve()), -1, -1))
    return tuple(stamp)


def clear_graph_runtime_cache() -> None:
    """Drop reusable graph references (primarily for worker shutdown/tests)."""

    _GRAPH_RUNTIME_CACHE.clear()

DATASET_ORDER = {"ccf": 0, "th_cpl": 1, "cas": 2, "jcr": 3}
DATASET_LEVELS = {
    "ccf": {"A", "B", "C"},
    "th_cpl": {"A", "B"},
    "cas": {"1", "2", "3", "4"},
    "jcr": {"Q1", "Q2", "Q3", "Q4", "N/A"},
}
DATASET_NAMES = {
    "ccf": "CCF",
    "th_cpl": "TH-CPL",
    "cas": "中科院",
    "jcr": "JCR",
}
LEVEL_PRIORITY = {
    "ccf": ["A", "B", "C"],
    "th_cpl": ["A", "B"],
    "cas": ["1", "2", "3", "4"],
    "jcr": ["Q1", "Q2", "Q3", "Q4"],
}
RECORD_TYPE_NAMES = {"conference": "会议", "journal": "期刊"}
ARTICLE_TYPE_NAMES = {
    "original_research": "原创研究",
    "systems_experience": "系统/部署经验",
    "theory_methods": "理论与方法",
    "dataset_benchmark": "数据集/基准",
    "survey_review": "综述/教程",
    "systematization_of_knowledge": "系统化知识/SoK",
    "applied_research": "应用研究",
    "industry_application": "工业应用",
}
SUBMISSION_MODE_NAMES = {
    "open": "常规开放投稿",
    "cfp_only": "仅对应征稿/专题",
    "invited_mixed": "邀请或专题为主",
    "proposal_first": "先提交选题提案",
    "invited_only": "仅限编辑邀请",
    "varies_by_series": "依具体分刊而定",
    "retired_merged": "已合并，不再独立投稿",
}
SCOPE_CONTEXT_NAMES = {
    "main_track": "主会研究轨",
    "venue": "会刊范围",
    "journal_first": "期刊优先/会议展示",
    "journal_proceedings": "会议审稿/期刊论文集",
    "journal_family": "期刊家族（需选择具体分刊）",
}
TARGET_STATUS_NAMES = {
    "active_target": "当前可投稿目标",
    "historical_merged": "历史实体，已合并",
    "family_non_actionable": "刊系占位，不可直接投稿",
}
CURATED_SCOPE_SOURCE_TYPES = {
    "official_cfp",
    "official_scope",
    "official_author_guidelines",
    "publisher_scope",
}
CURATED_TOPIC_TAGS = {
    "accelerators_heterogeneous",
    "accessibility",
    "affective_computing",
    "ai_for_science",
    "ai_systems",
    "algorithmic_economics_game_theory",
    "algorithms",
    "arch_microarchitecture",
    "automata_computation",
    "autonomous_vehicles",
    "beamforming_mimo",
    "bioinformatics_computational_biology",
    "bionics_biohybrid_systems",
    "compilers_runtime",
    "complexity",
    "computer_vision_pattern",
    "cognitive_communications",
    "control_systems",
    "cryptography",
    "cscw_social",
    "data_management",
    "data_mining",
    "data_science_engineering",
    "database_systems",
    "datacenter_networking",
    "deep_representation",
    "distributed_cloud_edge",
    "eda_cad",
    "edge_networking",
    "embedded_realtime_cps",
    "forensics_trust",
    "fpga_reconfigurable_computing",
    "formal_methods_verification",
    "general_computing",
    "generative_foundation_models",
    "geometric_cad_design",
    "graph_knowledge_data",
    "graphics_rendering_geometry",
    "hardware_security",
    "hci_ux",
    "image_video_processing",
    "industry_application",
    "industrial_informatics_manufacturing",
    "information_fusion",
    "information_retrieval",
    "information_theory_coding",
    "interaction_techniques",
    "iot_sensor",
    "knowledge_systems",
    "logic_semantics",
    "machine_learning",
    "multimedia",
    "multimodal",
    "network_arch_protocols",
    "network_security",
    "nlp_speech",
    "operating_systems",
    "optimization",
    "parallel_hpc",
    "performance_measurement",
    "physical_layer_communications",
    "privacy_anonymity",
    "probabilistic_causal",
    "program_synthesis",
    "programming_languages",
    "reinforcement_decision",
    "resource_allocation_scheduling",
    "robotics_autonomy",
    "scientific_computing",
    "software_engineering",
    "software_security",
    "storage_filesystems",
    "stream_spatiotemporal",
    "survey_review",
    "system_security",
    "testing_analysis",
    "theory_methods",
    "trustworthy_ai",
    "ubiquitous_mobile_wearable",
    "visualization_visual_analytics",
    "vr_ar",
    "web_search_recommendation",
    "web_social_networks",
    "wireless_mobile",
    "evolutionary_computation",
    "fuzzy_systems",
}
ORIGINAL_RESEARCH_INTENT_RE = re.compile(
    r"原创(?:研究|论文|实验)?|实验论文|研究论文|"
    r"\b(?:original\s+(?:research|article|paper)|research\s+(?:article|paper))\b",
    re.I,
)
REVIEW_ARTICLE_INTENT_RE = re.compile(
    r"(?:综述|评述|教程)(?:论文|文章|稿件)|(?:系统性|文献)综述|"
    r"(?:综述|教程)\s*(?:和|或|/)?\s*(?:综述|教程)|"
    r"\b(?:systematic|literature)\s+(?:survey|review)\b|"
    r"\b(?:survey|tutorial)\s+and\s+(?:survey|tutorial)\b|"
    r"\b(?:survey|review|tutorial)\s+(?:paper|article|manuscript)\b|"
    r"\b(?:systematic|literature)\s+review\b|\bsurvey\s+of\b|"
    r"\b(?:this|our)\s+(?:paper|article|work)\s+(?:is|presents?)\s+"
    r"(?:an?\s+)?(?:survey|review|tutorial)\b|"
    r"\bstate[ -]of[ -]the[ -]art\s+(?:survey|review)\b|"
    r"(?:综述|评述|教程)(?=$|[，,、；;：:])|"
    r"\b(?:survey|review|tutorial)\s*(?:paper|article|manuscript)?\s*$",
    re.I,
)
SOK_ARTICLE_INTENT_RE = re.compile(
    r"\b(?:systematization\s+of\s+knowledge|SoK)\b|系统化知识|知识系统化",
    re.I,
)
STRONG_REVIEW_DOCUMENT_RE = re.compile(
    r"(?:综述|评述|教程)(?:论文|文章|稿件)|"
    r"(?:综述|教程)\s*(?:和|或|/)?\s*(?:综述|教程)|"
    r"\b(?:survey|review|tutorial)\s+(?:paper|article|manuscript)\b|"
    r"\b(?:this|our)\s+(?:paper|article|work)\s+(?:is|presents?)\s+"
    r"(?:an?\s+)?(?:survey|review|tutorial)\b",
    re.I,
)
REVIEW_CONTEXT_RE = re.compile(
    r"(?:系统性|文献)综述|\b(?:systematic|literature)\s+review\b|"
    r"\bsurvey\s+of\b|\bstate[ -]of[ -]the[ -]art\s+(?:survey|review)\b",
    re.I,
)
ORIGINAL_CONTRIBUTION_CUE_RE = re.compile(
    r"本文(?:提出|介绍|设计|实现|评估)|我们(?:提出|介绍|设计|实现|评估)|"
    r"新的(?:算法|方法|系统|模型)|实验评估|"
    r"\b(?:we|this\s+(?:paper|work))\b.{0,160}?\b(?:propose|present|introduce|develop|"
    r"evaluate|design|implement|demonstrate)\b|"
    r"\b(?:novel|new)\s+(?:algorithm|method|system|model|approach)\b|\bexperiments?\b",
    re.I,
)
REVIEW_VERB_CUE_RE = re.compile(
    r"\b(?:we|this\s+(?:paper|work)|our\s+(?:paper|work))\s+"
    r"(?:review|summari[sz]e|survey|overview)\b|"
    r"\b(?:we|this\s+(?:paper|work))\s+(?:conduct|perform)\s+"
    r"(?:an?\s+)?(?:literature\s+)?review\b|"
    r"本文(?:回顾|综述|总结)",
    re.I,
)
NEGATIVE_SCOPE_CUE_RE = re.compile(
    r"(?:不涉及|没有|缺少|不含|无关|仅|只把|只用|纯(?:湿|粹)?|"
    r"不属于|不具备|without|no\b|lack(?:s|ing)?|only|just|pure(?:ly)?|"
    r"generic|general[- ]purpose)",
    re.I,
)
NEGATED_ORIGINAL_INTENT_RE = re.compile(
    r"(?:不是|并非|不属于|不写|不做|不投|不考虑|不要|不准备|非)\s*"
    r"(?:原创(?:研究|论文|实验)?|实验论文|研究论文|"
    r"original\s+(?:research|article|paper)|research\s+(?:article|paper))|"
    r"\bnot\s+(?:an?\s+)?(?:original\s+(?:research|article|paper)|"
    r"research\s+(?:article|paper))\b",
    re.I,
)
NEGATED_REVIEW_INTENT_RE = re.compile(
    r"(?:不是|并非|不属于|不写|不做|不投|不考虑|不要|不准备|非)\s*"
    r"(?:(?:综述|评述|教程)(?:论文|文章|稿件)?|"
    r"(?:survey|review|tutorial)(?:\s+(?:paper|article|manuscript))?)|"
    r"\bnot\s+(?:a\s+)?(?:survey|review|tutorial)(?:\s+(?:paper|article))?\b",
    re.I,
)
JOURNAL_EXACT_NAME_ALIASES = {
    "the vldb journal": "vldb journal",
    "ieee journal of selected areas in communications": "ieee journal on selected areas in communications",
    "ieee trans on pattern analysis and machine intelligence": (
        "ieee transactions on pattern analysis and machine intelligence"
    ),
}
JOURNAL_LINEAGE_ALIASES = {
    "vldb journal": "vldb_journal",
    "ieee transactions on audio speech and language processing": "taslp",
    "ieee acm transactions on audio speech and language processing": "taslp",
    "ieee acm transactions on networking": "ton",
    "ieee transactions on networking": "ton",
}
JOURNAL_DISPLAY_NAMES = {
    "vldb_journal": "The VLDB Journal",
    "taslp": "IEEE Transactions on Audio, Speech and Language Processing",
    "ton": "IEEE Transactions on Networking",
}
CONFERENCE_DISPLAY_NAMES = {
    "robotics science and systems a robotics conference": "Robotics: Science and Systems",
}
CONFERENCE_EXACT_NAME_ALIASES = {
    "acm sigops annual technical conference": "usenix annual technical conference",
    "usenix annul technical conference": "usenix annual technical conference",
    "acm sigsoft symposium on the foundation of software engineering european software engineering conference": (
        "acm international conference on the foundations of software engineering"
    ),
    "european cryptology conference": (
        "international conference on the theory and applications of cryptographic techniques"
    ),
    "acm conference on management of data": "acm sigmod conference",
    "computer aided verification": "international conference on computer aided verification",
    "acm siggraph annual conference": "acm special interest group on computer graphics",
    "ieee virtual reality": (
        "ieee conference on virtual reality and 3d user interfaces 原 ieee virtual reality"
    ),
}

# 将用户的中英文研究描述映射到审核数据中的受控 L2 方向标签。
# 规则只使用有辨识度的短语，避免把“系统”、“模型”等泛词当成学科意图。
QUERY_CONCEPT_RULES = (
    ("fpga_reconfigurable_computing", "FPGA/可重构计算", re.compile(r"现场可编程门阵列|可重构计算|\bFPGA\b|field[ -]programmable gate array|reconfigurable computing", re.I)),
    ("storage_filesystems", "文件/存储系统", re.compile(r"文件系统|存储系统|分布式存储|持久内存|固态(?:硬盘|盘)|掉电恢复|崩溃(?:恢复|一致性)|文件(?:目录|内容).{0,12}恢复|\bfile\s*systems?\b|\bstorage\s*systems?\b|persistent memory|solid[ -]state drives?|\bSSDs?\b|crash (?:recovery|consistency)|power[ -]loss recovery", re.I)),
    ("operating_systems", "操作系统", re.compile(r"操作系统|内核|\boperating\s*systems?\b|\bos\s+kernel\b", re.I)),
    ("datacenter_networking", "数据中心网络", re.compile(r"数据中心(?:网络|互联|拥塞)|data[ -]?cent(?:er|re)\s+(?:network|interconnect|congestion)", re.I)),
    ("network_arch_protocols", "网络架构与协议", re.compile(r"计算机网络|通信网络|网络协议|网络架构|拥塞控制|路由协议|无线(?:边缘)?网络|移动网络|computer network|communication network|network (?:architecture|protocol)|congestion control|routing protocol", re.I)),
    ("wireless_mobile", "无线与移动网络", re.compile(r"无线(?:通信|网络|系统|边缘)|移动网络|蜂窝网络|(?:手机|移动终端).{0,24}(?:信号|传输|链路)|(?:信号|链路).{0,24}(?:时好时坏|波动|自适应|传输策略)|wireless|mobile network|cellular network|weak(?:ly)? connected|intermittent connectivity|link adaptation", re.I)),
    ("physical_layer_communications", "通信物理层", re.compile(r"物理层|无线通信|无线信道|信道编码|调制编码|链路自适应|physical layer|wireless communication|wireless channel|channel coding|link adaptation", re.I)),
    ("information_theory_coding", "信息论与编码", re.compile(r"信息论|纠错码|信源编码|信道容量|information theory|error[ -]correcting code|source coding|channel capacity", re.I)),
    ("cognitive_communications", "认知/语义通信", re.compile(r"认知无线电|认知通信|语义通信|cognitive radio|cognitive communications?|semantic communication", re.I)),
    ("beamforming_mimo", "波束成形/MIMO", re.compile(r"波束(?:成形|赋形)?|\bbeamform(?:ing)?\b|\bMIMO\b", re.I)),
    ("resource_allocation_scheduling", "资源分配与调度", re.compile(r"资源分配|功率控制|任务调度|实时调度|resource allocation|power control|task scheduling|real-time scheduling", re.I)),
    ("edge_networking", "边缘网络/计算", re.compile(r"边缘网络|边缘计算|移动边缘|edge (?:network|computing)|mobile edge", re.I)),
    ("embedded_realtime_cps", "实时/嵌入式/CPS", re.compile(r"实时系统|可调度性|时序保证|嵌入式系统|信息物理系统|real-time system|schedulability|timing guarantee|cyber-physical system", re.I)),
    ("generative_foundation_models", "生成式/基础模型", re.compile(r"大语言模型|基础模型|生成式(?:AI|人工智能)?|\bLLMs?\b|foundation model|generative (?:AI|model)", re.I)),
    ("nlp_speech", "NLP/语音", re.compile(r"自然语言处理|语言模型|机器翻译|语音识别|文本生成|\bNLP\b|language model|machine translation|speech recognition|text generation", re.I)),
    ("machine_learning", "机器学习", re.compile(r"机器学习|大语言模型|\bmachine learning\b|\bLLMs?\b", re.I)),
    ("deep_representation", "深度表示学习", re.compile(r"深度学习|神经网络|表示学习|deep learning|neural network|representation learning", re.I)),
    ("ai_systems", "AI/机器学习系统", re.compile(r"AI系统|机器学习系统|模型训练系统|分布式训练|训练基础设施|大规模模型训练|AI systems?|machine learning systems?|distributed training|training infrastructure", re.I)),
    ("probabilistic_causal", "概率与因果推断", re.compile(r"因果推断|因果效应|因果关系|反事实|观察(?:性)?数据.{0,40}(?:导致|治疗|干预|因果)|causal inference|causal effect|causal relation|counterfactual|observational data.{0,50}(?:cause|treatment|intervention)", re.I)),
    ("reinforcement_decision", "强化学习/决策", re.compile(r"强化学习|序列决策|reinforcement learning|sequential decision", re.I)),
    ("control_systems", "控制系统", re.compile(r"控制系统|自动控制|控制理论|系统辨识|control systems?|control theory|system identification", re.I)),
    ("evolutionary_computation", "进化计算", re.compile(r"进化(?:计算|算法)|演化(?:计算|算法)|遗传算法|群体智能|evolutionary (?:computation|algorithm)|genetic algorithm|swarm intelligence", re.I)),
    ("fuzzy_systems", "模糊系统", re.compile(r"模糊系统|模糊逻辑|模糊控制|fuzzy systems?|fuzzy logic|fuzzy control", re.I)),
    ("affective_computing", "情感计算", re.compile(r"情感计算|情绪识别|情感识别|affective computing|emotion recognition", re.I)),
    ("vr_ar", "VR/AR", re.compile(r"虚拟现实|增强现实|混合现实|头显|头戴式显示|眩晕|三维空间.{0,16}(?:手势|交互)|\bVR\b|\bAR\b|virtual reality|augmented reality|mixed reality|head[ -]mounted display|motion sickness", re.I)),
    ("hci_ux", "人机交互", re.compile(r"人机交互|用户体验|虚拟现实.{0,8}交互|手势(?:交互|操作)|交互手势|human[ -]computer interaction|user experience|gesture interaction|\bHCI\b", re.I)),
    ("trustworthy_ai", "可信AI", re.compile(r"可信(?:AI|人工智能)|可解释(?:AI|人工智能)|算法公平|对抗鲁棒|trustworthy AI|explainable AI|algorithmic fairness|adversarial robustness", re.I)),
    ("computer_vision_pattern", "计算机视觉", re.compile(r"计算机视觉|目标检测|图像识别|视觉识别|computer vision|object detection|image recognition", re.I)),
    ("multimodal", "多模态学习", re.compile(r"多模态|图文(?:理解|生成|检索)|视觉问答|图片.{0,24}(?:语音|文本|回答)|multimodal|vision[ -]language|visual question answering|(?:image|vision).{0,36}(?:speech|text|question answering)", re.I)),
    ("image_video_processing", "图像/视频处理", re.compile(r"图像处理|视频处理|图像压缩|视频编码|image processing|video processing|image compression|video coding", re.I)),
    ("geometric_cad_design", "几何CAD", re.compile(r"几何(?:造型|设计)|曲面(?:建模|设计)|机械CAD|geometric (?:design|modeling)|surface (?:design|modeling)", re.I)),
    ("software_engineering", "软件工程", re.compile(r"软件工程|软件测试|软件维护|software engineering|software testing|software maintenance", re.I)),
    ("testing_analysis", "程序分析与测试", re.compile(r"程序分析|静态分析|动态分析|模糊测试|自动.{0,12}发现.{0,24}(?:漏洞|内存越界)|自动(?:生成|合成).{0,12}(?:补丁|修复)|程序(?:自动)?修复|program analysis|static analysis|dynamic analysis|fuzz(?:ing)?|automated program repair|patch generation", re.I)),
    ("formal_methods_verification", "形式化方法与验证", re.compile(r"形式化方法|模型检验|形式验证|formal methods?|model checking|formal verification", re.I)),
    ("software_security", "软件安全", re.compile(r"软件安全|内存安全|内存越界|缓冲区溢出|释放后使用|漏洞.{0,20}(?:修复|补丁)|software security|memory safety|memory corruption|buffer overflow|use[ -]after[ -]free|vulnerabilit(?:y|ies).{0,30}(?:repair|patch)", re.I)),
    ("system_security", "系统安全", re.compile(r"系统安全|漏洞利用|恶意软件|入侵检测|内存越界|缓冲区溢出|system security|vulnerability exploit|malware|intrusion detection|memory corruption|buffer overflow", re.I)),
    ("cryptography", "密码学", re.compile(r"密码学|加密协议|零知识证明|cryptograph|zero-knowledge proof", re.I)),
    ("privacy_anonymity", "隐私与匿名", re.compile(r"隐私保护|差分隐私|匿名通信|位置轨迹.{0,24}(?:隐藏|泄露|保护|隐私)|(?:隐藏|保护).{0,24}位置轨迹|privacy preserving|differential privacy|anonymous communication|(?:protect\w*|privat\w*|without revealing).{0,50}location traces?|location traces?.{0,50}(?:protect\w*|privat\w*|without revealing)", re.I)),
    ("data_management", "数据库与数据管理", re.compile(r"数据库|查询优化|事务处理|database|query optimization|transaction processing", re.I)),
    ("data_mining", "数据挖掘", re.compile(r"数据挖掘|知识发现|data mining|knowledge discovery", re.I)),
    ("data_science_engineering", "数据科学与工程", re.compile(r"数据科学|数据工程|数据治理|data science|data engineering|data governance", re.I)),
    ("parallel_hpc", "并行与高性能计算", re.compile(r"并行计算|高性能计算|异构计算|(?:数百|数千|大规模).{0,18}(?:GPU|加速卡)|GPU.{0,36}(?:集群|并行|通信|数据交换|分布式训练)|parallel computing|high[ -]performance computing|heterogeneous computing|distributed GPU|GPU cluster|large[ -]scale training|\bHPC\b", re.I)),
    ("arch_microarchitecture", "体系结构/微结构", re.compile(r"计算机体系结构|处理器微结构|缓存一致性|computer architecture|processor microarchitecture|cache coherence", re.I)),
    ("robotics_autonomy", "机器人与自主系统", re.compile(r"机器人|自主系统|机器人感知|机器人控制|robotics?|autonomous systems?", re.I)),
    ("autonomous_vehicles", "智能/自动驾驶车辆", re.compile(r"智能车辆|自动驾驶|无人驾驶|autonomous vehicles?|self[ -]driving|intelligent vehicles?", re.I)),
    ("bioinformatics_computational_biology", "生物信息/计算生物学", re.compile(r"生物信息学|计算生物学|基因组学|蛋白质组学|bioinformatics|computational biology|genomics|proteomics", re.I)),
    ("bionics_biohybrid_systems", "仿生/生物混合系统", re.compile(r"仿生(?:系统|机器人)|生物混合系统|赛博格|脑机接口|\bbionics?\b|\bbiohybrid\b|\bcyborgs?\b|brain[ -]computer interface", re.I)),
    ("industrial_informatics_manufacturing", "工业信息/智能制造", re.compile(r"工业信息学|智能制造|工业物联网|计算机集成制造|industrial informatics|smart manufacturing|industrial (?:internet of things|IoT)|computer[ -]integrated manufacturing", re.I)),
    ("algorithmic_economics_game_theory", "算法经济学/博弈论", re.compile(r"算法博弈论|计算经济学|机制设计|拍卖算法|algorithmic game theory|computational economics|mechanism design|auction algorithm", re.I)),
    ("graphics_rendering_geometry", "计算机图形学", re.compile(r"计算机图形学|图形渲染|几何处理|computer graphics|graphics rendering|geometry processing", re.I)),
)
QUERY_CONCEPT_LABELS = {
    topic_tag: topic_tag.replace("_", " ") for topic_tag in CURATED_TOPIC_TAGS
}
QUERY_CONCEPT_LABELS.update(
    {topic_tag: display_name for topic_tag, display_name, _pattern in QUERY_CONCEPT_RULES}
)
QUERY_CONCEPT_WEIGHTS = {
    # 专指方向比宽泛的网络/调度概念更能区分投稿目标。
    "beamforming_mimo": 7.0,
    "physical_layer_communications": 4.0,
    "storage_filesystems": 4.0,
    "datacenter_networking": 4.0,
    "generative_foundation_models": 4.0,
    "trustworthy_ai": 4.0,
    "vr_ar": 4.0,
    "hci_ux": 4.0,
    "wireless_mobile": 5.0,
    "software_security": 4.0,
    "privacy_anonymity": 4.0,
    "probabilistic_causal": 4.0,
    "multimodal": 4.0,
    "ai_systems": 4.0,
    "parallel_hpc": 4.0,
    "network_arch_protocols": 2.5,
    "edge_networking": 2.5,
    "resource_allocation_scheduling": 1.5,
}
GENERIC_QUERY_CONCEPTS = {
    "resource_allocation_scheduling",
    "machine_learning",
    "optimization",
    "theory_methods",
    "general_computing",
}

MISSING_VALUES = {"", "n/a", "na", "null", "none", "-"}
ASCII_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#.]*", re.I)
CJK_SEQUENCE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
TARGET_SPLIT_RE = re.compile(r"[,，;；、|]+|(?:或者|或)|\s+or\s+", re.I)

ENGLISH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "article",
    "are",
    "as",
    "at",
    "be",
    "by",
    "conference",
    "for",
    "from",
    "journal",
    "in",
    "is",
    "of",
    "on",
    "or",
    "paper",
    "project",
    "research",
    "submission",
    "study",
    "that",
    "the",
    "this",
    "to",
    "using",
    "venue",
    "we",
    "with",
}
CJK_STOP_TOKENS = {
    "一种",
    "为了",
    "以及",
    "提出",
    "本文",
    "本项",
    "会刊",
    "会议",
    "文章",
    "期刊",
    "稿件",
    "投稿",
    "论文",
    "项目",
    "研究",
    "通过",
    "采用",
    "针对",
}


@dataclass(frozen=True)
class TargetSpec:
    dataset: str
    level: str

    @property
    def key(self) -> tuple[str, str]:
        return self.dataset, self.level

    @property
    def label(self) -> str:
        return ranking_label(self.dataset, self.level)


@dataclass(frozen=True)
class RankingStatistics:
    """Query-specific corpus statistics supplied by the persistent index."""

    total_documents: int
    reviewed_documents: int
    document_frequency: Mapping[str, int]
    concept_document_frequency: Mapping[str, int]


@dataclass(frozen=True)
class CuratedVenueScope:
    scope_id: str
    match_dataset: str
    match_version_year: str
    match_record_type: str
    match_name: str
    match_abbreviation: str
    scope_summary: str
    topic_tags: str
    keywords_zh: str
    keywords_en: str
    article_types: str
    accepts_original_research: str
    submission_mode: str
    scope_context: str
    scope_year: str
    out_of_scope: str
    source_type: str
    source_url: str
    secondary_source_urls: str
    source_accessed_at: str
    evidence: str
    review_status: str
    reviewed_by: str
    reviewed_at: str
    review_notes: str
    target_status: str

    @property
    def matching_text(self) -> str:
        return " ".join(
            (self.scope_summary, self.topic_tags, self.keywords_zh, self.keywords_en)
        )


@dataclass(frozen=True)
class VenueRecord:
    row_id: int
    dataset: str
    source: str
    source_file: str
    version_year: str
    record_type: str
    name: str
    abbreviation: str
    issn: str
    eissn: str
    area: str
    area_en: str
    level: str
    taxonomy_scope: str
    official_scope: str
    official_scope_url: str
    official_scope_status: str
    official_scope_confidence: str
    curated_scope_id: str
    curated_scope: str
    curated_topics_zh: str
    curated_topics_en: str
    curated_topic_tags: str
    curated_article_types: str
    curated_accepts_original_research: str
    curated_submission_mode: str
    curated_scope_context: str
    curated_scope_year: str
    curated_out_of_scope: str
    curated_scope_basis: str
    curated_scope_status: str
    curated_secondary_source_urls: str
    curated_target_status: str
    top: str
    impact_factor: str

    @property
    def target_key(self) -> tuple[str, str]:
        return self.dataset, self.level


@dataclass
class VenueCandidate:
    records: list[VenueRecord]
    matched_records: list[VenueRecord]
    score: float = 0.0
    matched_terms: list[str] = field(default_factory=list)
    matched_fields: list[str] = field(default_factory=list)
    matched_concepts: list[str] = field(default_factory=list)
    semantic_similarity: float | None = None
    graph_relevance: float | None = None
    graph_path: list[str] = field(default_factory=list)
    lightrag_relevance: float | None = None
    lightrag_channels: list[str] = field(default_factory=list)
    api_relevance: float | None = None
    api_confidence: str = ""
    api_reason: str = ""
    api_evidence_urls: list[str] = field(default_factory=list)

    @property
    def record_type(self) -> str:
        return self.records[0].record_type

    @property
    def name(self) -> str:
        lineage_names = {
            journal_lineage_name(record.name)
            for record in self.records
            if record.record_type == "journal"
        }
        for lineage_name in lineage_names:
            display_name = JOURNAL_DISPLAY_NAMES.get(lineage_name)
            if display_name:
                return display_name
        conference_names = {
            normalize_name(record.name)
            for record in self.records
            if record.record_type == "conference"
        }
        for conference_name in conference_names:
            display_name = CONFERENCE_DISPLAY_NAMES.get(conference_name)
            if display_name:
                return display_name
        choices = [
            record.name
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
        ]
        choices = choices or [record.name for record in self.matched_records]
        choices = choices or [record.name for record in self.records]
        return sorted(choices, key=_name_display_quality)[0]

    @property
    def abbreviation(self) -> str:
        values = _unique(record.abbreviation for record in self.records if record.abbreviation)
        return values[0] if values else ""

    @property
    def matched_ranking_labels(self) -> list[str]:
        return _ranking_labels(self.matched_records)

    @property
    def all_ranking_labels(self) -> list[str]:
        return _ranking_labels(self.records)

    @property
    def areas(self) -> list[str]:
        values = []
        for record in self.records:
            if record.area and record.area_en:
                values.append(f"{record.area} / {record.area_en}")
            elif record.area:
                values.append(record.area)
            elif record.area_en:
                values.append(record.area_en)
        return _unique(values)

    @property
    def taxonomy_scopes(self) -> list[str]:
        return _unique(record.taxonomy_scope for record in self.records if record.taxonomy_scope)

    @property
    def official_scope_candidates(self) -> list[str]:
        return _unique(
            record.official_scope
            for record in self.records
            if record.official_scope_status == "ok" and record.official_scope
        )

    @property
    def curated_scopes(self) -> list[str]:
        return _unique(
            record.curated_scope
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES and record.curated_scope
        )

    @property
    def curated_topics(self) -> list[str]:
        topics = []
        for record in self.records:
            if record.curated_scope_status not in CURATED_SCOPE_ACTIVE_STATUSES:
                continue
            topics.extend(_split_terms(record.curated_topics_zh))
            topics.extend(_split_terms(record.curated_topics_en))
        return _unique(topics)

    @property
    def curated_topic_tags(self) -> list[str]:
        return _unique(
            topic_tag
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            for topic_tag in _split_terms(record.curated_topic_tags)
        )

    @property
    def curated_article_types(self) -> list[str]:
        return _unique(
            article_type
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            for article_type in _split_terms(record.curated_article_types)
        )

    @property
    def curated_scope_contexts(self) -> list[str]:
        return _unique(
            record.curated_scope_context
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            and record.curated_scope_context
        )

    @property
    def curated_accepts_original_research(self) -> list[str]:
        return _unique(
            record.curated_accepts_original_research
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            and record.curated_accepts_original_research
        )

    @property
    def curated_submission_modes(self) -> list[str]:
        return _unique(
            record.curated_submission_mode
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            and record.curated_submission_mode
        )

    @property
    def curated_scope_years(self) -> list[str]:
        return _unique(
            record.curated_scope_year
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            and record.curated_scope_year
        )

    @property
    def curated_out_of_scope(self) -> list[str]:
        return _unique(
            record.curated_out_of_scope
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            and record.curated_out_of_scope
        )

    @property
    def curated_scope_bases(self) -> list[str]:
        return _unique(
            record.curated_scope_basis
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            and record.curated_scope_basis
        )

    @property
    def curated_secondary_source_urls(self) -> list[str]:
        return _unique(
            url
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            for url in _split_terms(record.curated_secondary_source_urls)
        )

    @property
    def curated_target_statuses(self) -> list[str]:
        return _unique(
            record.curated_target_status
            for record in self.records
            if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            and record.curated_target_status
        )

    @property
    def curated_scope_entries(self) -> list[dict[str, object]]:
        entries = []
        seen = set()
        for record in self.records:
            if (
                record.curated_scope_status not in CURATED_SCOPE_ACTIVE_STATUSES
                or not record.curated_scope_id
                or record.curated_scope_id in seen
            ):
                continue
            seen.add(record.curated_scope_id)
            entries.append(
                {
                    "scope_id": record.curated_scope_id,
                    "summary": record.curated_scope,
                    "topics": [
                        *_split_terms(record.curated_topics_zh),
                        *_split_terms(record.curated_topics_en),
                    ],
                    "topic_tags": _split_terms(record.curated_topic_tags),
                    "article_types": _split_terms(record.curated_article_types),
                    "accepts_original_research": record.curated_accepts_original_research,
                    "submission_mode": record.curated_submission_mode,
                    "scope_context": record.curated_scope_context,
                    "scope_year": record.curated_scope_year,
                    "out_of_scope": record.curated_out_of_scope,
                    "source_type": record.curated_scope_basis,
                    "secondary_source_urls": _split_terms(record.curated_secondary_source_urls),
                    "target_status": record.curated_target_status,
                }
            )
        return entries

    @property
    def is_top(self) -> bool:
        return any(record.top == "是" for record in self.records)

    @property
    def impact_factors(self) -> list[str]:
        return _unique(
            record.impact_factor
            for record in self.records
            if record.impact_factor.casefold() not in MISSING_VALUES
        )

    def matching_document(self, include_official_scope: bool) -> dict[str, str]:
        document = {
            "name": " ".join(_unique(record.name for record in self.records)),
            "abbreviation": " ".join(_unique(record.abbreviation for record in self.records if record.abbreviation)),
            "area": " ".join(self.areas),
            "taxonomy_scope": " ".join(self.taxonomy_scopes),
            "curated_scope": " ".join(self.curated_scopes),
            "curated_topics": " ".join((*self.curated_topics, *self.curated_topic_tags)),
            "official_scope": "",
        }
        if include_official_scope:
            document["official_scope"] = " ".join(self.official_scope_candidates)
        return document


class DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_name(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", normalize_space(value)).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+", " ", value)
    return normalize_space(value)


def normalize_search_text(value: str | None) -> str:
    """归一化搜索文本，保留 C++、C# 和 .NET 等有语义的技术符号。"""

    value = unicodedata.normalize("NFKC", normalize_space(value)).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w\u3400-\u4dbf\u4e00-\u9fff+#.]+", " ", value)
    return normalize_space(value)


def _matches_search_filter(document: str, filter_value: str) -> bool:
    normalized_filter = normalize_search_text(filter_value)
    normalized_document = normalize_search_text(document)
    if not normalized_filter:
        return False
    if CJK_SEQUENCE_RE.search(normalized_filter):
        return normalized_filter in normalized_document
    technical_word = r"a-z0-9_+#."
    return bool(
        re.search(
            rf"(?<![{technical_word}]){re.escape(normalized_filter)}(?![{technical_word}])",
            normalized_document,
            re.I,
        )
    )


def valid_issn_token(value: str | None) -> str:
    raw = normalize_space(value).upper()
    if raw.casefold() in MISSING_VALUES:
        return ""
    if not re.fullmatch(r"\d{4}-?\d{3}[\dX]", raw):
        return ""
    token = raw.replace("-", "")
    total = sum(int(token[index]) * (8 - index) for index in range(7))
    check = (11 - total % 11) % 11
    expected = "X" if check == 10 else str(check)
    return token if token[-1] == expected else ""


def journal_identity_name(value: str | None) -> str:
    name = normalize_name(value)
    return JOURNAL_EXACT_NAME_ALIASES.get(name, name)


def journal_lineage_name(value: str | None) -> str:
    return JOURNAL_LINEAGE_ALIASES.get(journal_identity_name(value), journal_identity_name(value))


def conference_identity_name(value: str | None) -> str:
    name = normalize_name(value)
    return CONFERENCE_EXACT_NAME_ALIASES.get(name, name)


def _split_terms(value: str | None) -> list[str]:
    return [
        normalize_space(term)
        for term in re.split(r"[;；|]+", value or "")
        if normalize_space(term)
    ]


def _article_intents(query: str) -> tuple[bool, bool]:
    """识别显式稿件类型意图，并排除“不是原创/不是综述”等否定表达。"""

    normalized = unicodedata.normalize("NFKC", query)
    explicit_original_negation = bool(NEGATED_ORIGINAL_INTENT_RE.search(normalized))
    original_text = NEGATED_ORIGINAL_INTENT_RE.sub(" ", normalized)
    review_text = NEGATED_REVIEW_INTENT_RE.sub(" ", normalized)
    review_intent = bool(
        REVIEW_ARTICLE_INTENT_RE.search(review_text)
        or SOK_ARTICLE_INTENT_RE.search(review_text)
    )
    if (
        review_intent
        and REVIEW_CONTEXT_RE.search(review_text)
        and not STRONG_REVIEW_DOCUMENT_RE.search(review_text)
        and ORIGINAL_CONTRIBUTION_CUE_RE.search(review_text)
    ):
        review_intent = False
    explicit_original = bool(ORIGINAL_RESEARCH_INTENT_RE.search(original_text))
    implicit_original = bool(
        ORIGINAL_CONTRIBUTION_CUE_RE.search(original_text)
        and not REVIEW_VERB_CUE_RE.search(original_text)
        and not REVIEW_CONTEXT_RE.search(original_text)
        and not STRONG_REVIEW_DOCUMENT_RE.search(review_text)
    )
    return (
        explicit_original or (implicit_original and not explicit_original_negation),
        review_intent,
    )


def detect_query_concepts(query: str) -> list[tuple[str, str]]:
    """识别查询中可与审核 L2 标签对齐的研究方向。"""

    normalized = unicodedata.normalize("NFKC", query).casefold()
    return [
        (topic_tag, display_name)
        for topic_tag, display_name, pattern in QUERY_CONCEPT_RULES
        if pattern.search(normalized)
    ]


def merge_query_concepts(
    query: str,
    additional: Sequence[tuple[str, str]] = (),
) -> list[tuple[str, str]]:
    """合并本地规则和受约束 API 标签，保留稳定顺序。"""

    concepts = detect_query_concepts(query)
    seen = {topic_tag for topic_tag, _label in concepts}
    for topic_tag, label in additional:
        if topic_tag not in CURATED_TOPIC_TAGS or topic_tag in seen:
            continue
        seen.add(topic_tag)
        concepts.append((topic_tag, label or QUERY_CONCEPT_LABELS[topic_tag]))
    return concepts


def load_curated_scopes(
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[tuple[str, str, str, str], CuratedVenueScope]:
    """读取审核范围；使用榜单、版本、类型、完整名称四元组严格锚定。"""

    path = data_dir / CURATED_SCOPE_FILE
    if not path.exists():
        return {}
    required = {
        "scope_id",
        "match_dataset",
        "match_version_year",
        "match_record_type",
        "match_name",
        "match_abbreviation",
        "scope_summary",
        "topic_tags",
        "keywords_zh",
        "keywords_en",
        "article_types",
        "accepts_original_research",
        "submission_mode",
        "scope_context",
        "scope_year",
        "out_of_scope",
        "source_type",
        "source_url",
        "secondary_source_urls",
        "source_accessed_at",
        "evidence",
        "review_status",
        "reviewed_by",
        "reviewed_at",
        "review_notes",
        "target_status",
    }
    index: dict[tuple[str, str, str, str], CuratedVenueScope] = {}
    scope_ids: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        missing_fields = sorted(required - set(reader.fieldnames or []))
        if missing_fields:
            raise ValueError(f"{path} 缺少字段：{', '.join(missing_fields)}")
        for row_number, row in enumerate(reader, start=2):
            scope = CuratedVenueScope(
                scope_id=normalize_space(row.get("scope_id")),
                match_dataset=normalize_space(row.get("match_dataset")),
                match_version_year=normalize_space(row.get("match_version_year")),
                match_record_type=normalize_space(row.get("match_record_type")),
                match_name=normalize_space(row.get("match_name")),
                match_abbreviation=normalize_space(row.get("match_abbreviation")),
                scope_summary=normalize_space(row.get("scope_summary")),
                topic_tags=normalize_space(row.get("topic_tags")),
                keywords_zh=normalize_space(row.get("keywords_zh")),
                keywords_en=normalize_space(row.get("keywords_en")),
                article_types=normalize_space(row.get("article_types")),
                accepts_original_research=normalize_space(row.get("accepts_original_research")),
                submission_mode=normalize_space(row.get("submission_mode")),
                scope_context=normalize_space(row.get("scope_context")),
                scope_year=normalize_space(row.get("scope_year")),
                out_of_scope=normalize_space(row.get("out_of_scope")),
                source_type=normalize_space(row.get("source_type")),
                source_url=normalize_space(row.get("source_url")),
                secondary_source_urls=normalize_space(row.get("secondary_source_urls")),
                source_accessed_at=normalize_space(row.get("source_accessed_at")),
                evidence=normalize_space(row.get("evidence")),
                review_status=normalize_space(row.get("review_status")),
                reviewed_by=normalize_space(row.get("reviewed_by")),
                reviewed_at=normalize_space(row.get("reviewed_at")),
                review_notes=normalize_space(row.get("review_notes")),
                target_status=normalize_space(row.get("target_status")),
            )
            if not scope.scope_id or not scope.match_name:
                raise ValueError(f"{path}:{row_number} 的 scope_id、match_name 不能为空")
            if scope.scope_id in scope_ids:
                raise ValueError(f"{path}:{row_number} 的 scope_id 重复：{scope.scope_id!r}")
            scope_ids.add(scope.scope_id)
            if scope.match_record_type not in RECORD_TYPE_NAMES:
                raise ValueError(
                    f"{path}:{row_number} 的 match_record_type 无效：{scope.match_record_type!r}"
                )
            if scope.match_dataset not in DATASET_LEVELS:
                raise ValueError(f"{path}:{row_number} 的 match_dataset 无效：{scope.match_dataset!r}")
            if scope.review_status not in CURATED_SCOPE_VALID_STATUSES:
                raise ValueError(f"{path}:{row_number} 的 review_status 无效：{scope.review_status!r}")
            if scope.review_status not in CURATED_SCOPE_ACTIVE_STATUSES:
                continue
            if not scope.scope_summary:
                raise ValueError(f"{path}:{row_number} 的 approved 范围缺少 scope_summary")
            if not all(
                (
                    scope.topic_tags,
                    scope.keywords_zh,
                    scope.keywords_en,
                    scope.article_types,
                    scope.accepts_original_research,
                    scope.submission_mode,
                    scope.scope_year,
                    scope.out_of_scope,
                    scope.source_type,
                    scope.source_url,
                    scope.target_status,
                    scope.source_accessed_at,
                    scope.evidence,
                    scope.reviewed_by,
                    scope.reviewed_at,
                    scope.review_notes,
                )
            ):
                raise ValueError(f"{path}:{row_number} 的 approved 范围缺少来源或审核信息")
            topic_tags = set(_split_terms(scope.topic_tags))
            invalid_topic_tags = sorted(topic_tags - CURATED_TOPIC_TAGS)
            if invalid_topic_tags:
                raise ValueError(
                    f"{path}:{row_number} 的 topic_tags 无效："
                    + ", ".join(invalid_topic_tags)
                )
            article_types = set(_split_terms(scope.article_types))
            invalid_article_types = sorted(article_types - set(ARTICLE_TYPE_NAMES))
            if invalid_article_types:
                raise ValueError(
                    f"{path}:{row_number} 的 article_types 无效："
                    + ", ".join(invalid_article_types)
                )
            if scope.accepts_original_research not in {"yes", "no", "unknown"}:
                raise ValueError(
                    f"{path}:{row_number} 的 accepts_original_research 无效："
                    f"{scope.accepts_original_research!r}"
                )
            if (
                scope.accepts_original_research == "yes"
                and "original_research" not in article_types
            ) or (
                scope.accepts_original_research == "no"
                and "original_research" in article_types
            ):
                raise ValueError(
                    f"{path}:{row_number} 的 article_types 与 accepts_original_research 矛盾"
                )
            if scope.submission_mode not in SUBMISSION_MODE_NAMES:
                raise ValueError(
                    f"{path}:{row_number} 的 submission_mode 无效：{scope.submission_mode!r}"
                )
            if scope.scope_context not in SCOPE_CONTEXT_NAMES:
                raise ValueError(
                    f"{path}:{row_number} 的 scope_context 无效：{scope.scope_context!r}"
                )
            if scope.target_status not in TARGET_STATUS_NAMES:
                raise ValueError(
                    f"{path}:{row_number} 的 target_status 无效：{scope.target_status!r}"
                )
            if (
                scope.target_status == "historical_merged"
                and scope.submission_mode != "retired_merged"
            ):
                raise ValueError(
                    f"{path}:{row_number} 的历史合并目标必须使用 submission_mode=retired_merged"
                )
            if (
                scope.target_status == "family_non_actionable"
                and scope.scope_context != "journal_family"
            ):
                raise ValueError(
                    f"{path}:{row_number} 的刊系占位项必须使用 scope_context=journal_family"
                )
            if (
                scope.target_status == "active_target"
                and scope.submission_mode == "retired_merged"
            ):
                raise ValueError(
                    f"{path}:{row_number} 的 active_target 不能使用 submission_mode=retired_merged"
                )
            if scope.source_type not in CURATED_SCOPE_SOURCE_TYPES:
                raise ValueError(
                    f"{path}:{row_number} 的 source_type 无效：{scope.source_type!r}"
                )
            if not re.fullmatch(r"\d{4}", scope.match_version_year) or not re.fullmatch(
                r"\d{4}", scope.scope_year
            ):
                raise ValueError(f"{path}:{row_number} 的榜单年份或范围年份必须是四位数")
            for field_name, value in (
                ("source_accessed_at", scope.source_accessed_at),
                ("reviewed_at", scope.reviewed_at),
            ):
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError(
                        f"{path}:{row_number} 的 {field_name} 必须是 YYYY-MM-DD 有效日期"
                    ) from exc
            if not re.match(r"https?://", scope.source_url, re.I):
                raise ValueError(f"{path}:{row_number} 的 source_url 必须是 HTTP(S) URL")
            for secondary_url in _split_terms(scope.secondary_source_urls):
                if not re.match(r"https?://", secondary_url, re.I):
                    raise ValueError(
                        f"{path}:{row_number} 的 secondary_source_urls 包含无效 URL：{secondary_url!r}"
                    )
            key = (
                scope.match_dataset,
                scope.match_version_year,
                scope.match_record_type,
                normalize_name(scope.match_name),
            )
            if key in index:
                raise ValueError(f"{path}:{row_number} 与 {index[key].scope_id!r} 使用了相同锚点")
            index[key] = scope
    return index


def ranking_label(dataset: str, level: str, year: str = "") -> str:
    if dataset == "cas":
        label = f"中科院{level}区"
    elif dataset == "jcr":
        label = f"JCR-{level}"
    else:
        label = f"{DATASET_NAMES.get(dataset, dataset)}-{level}"
    return f"{label}（{year}）" if year else label


def parse_target(value: str) -> TargetSpec:
    normalized = unicodedata.normalize("NFKC", value).strip()
    compact = re.sub(r"[\s_:\-]+", "", normalized).upper()
    compact = compact.removesuffix("类")

    match = re.fullmatch(r"CCF([ABC])", compact)
    if match:
        return TargetSpec("ccf", match.group(1))

    match = re.fullmatch(r"(?:THCPL|TH)([AB])", compact)
    if match:
        return TargetSpec("th_cpl", match.group(1))

    chinese_numbers = {"一": "1", "二": "2", "三": "3", "四": "4"}
    match = re.fullmatch(r"(?:CAS|中科院(?:大类)?)([1234一二三四])区?", compact)
    if match:
        level = chinese_numbers.get(match.group(1), match.group(1))
        return TargetSpec("cas", level)

    match = re.fullmatch(r"JCR(?:主类别|第一类别)?Q?([1234])", compact)
    if match:
        return TargetSpec("jcr", f"Q{match.group(1)}")

    raise ValueError(
        f"无法识别目标等级 {value!r}；示例：CCF-A、THCPL-A、中科院1区、JCR-Q1"
    )


def parse_targets(values: Sequence[str]) -> list[TargetSpec]:
    targets = []
    seen = set()
    for value in values:
        for item in TARGET_SPLIT_RE.split(value):
            item = item.strip()
            if not item:
                continue
            include_better = item.endswith("及以上")
            if include_better:
                item = item[: -len("及以上")].strip()
            target = parse_target(item)
            expanded = [target]
            if include_better:
                priorities = LEVEL_PRIORITY[target.dataset]
                expanded = [
                    TargetSpec(target.dataset, level)
                    for level in priorities[: priorities.index(target.level) + 1]
                ]
            for expanded_target in expanded:
                if expanded_target.key not in seen:
                    seen.add(expanded_target.key)
                    targets.append(expanded_target)
    if not targets:
        raise ValueError("至少需要一个目标等级")
    return targets


def load_records(data_dir: Path = DEFAULT_DATA_DIR) -> list[VenueRecord]:
    records: list[VenueRecord] = []
    curated_scopes = load_curated_scopes(data_dir)
    matched_curated_keys: set[tuple[str, str, str, str]] = set()
    matched_curated_rows: dict[tuple[str, str, str, str], list[int]] = defaultdict(list)
    required = {
        "dataset",
        "source",
        "source_file",
        "version_year",
        "record_type",
        "name",
        "level",
        "area",
        "收稿方向",
    }
    for filename in DATA_FILES:
        path = data_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"缺少数据文件：{path}")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing_fields = sorted(required - set(reader.fieldnames or []))
            if missing_fields:
                raise ValueError(f"{path} 缺少字段：{', '.join(missing_fields)}")
            for source_row, row in enumerate(reader, start=1):
                record_type = normalize_space(row.get("record_type"))
                name = normalize_space(row.get("name")).replace("\x7f", " ")
                dataset = normalize_space(row.get("dataset"))
                version_year = normalize_space(row.get("version_year"))
                curated_key = (dataset, version_year, record_type, normalize_name(name))
                curated = curated_scopes.get(curated_key)
                abbreviation = normalize_space(row.get("abbreviation"))
                if curated:
                    matched_curated_keys.add(curated_key)
                    matched_curated_rows[curated_key].append(len(records))
                    expected_abbreviation = re.sub(
                        r"[^a-z0-9]+", "", curated.match_abbreviation.casefold()
                    )
                    actual_abbreviation = re.sub(r"[^a-z0-9]+", "", abbreviation.casefold())
                    if expected_abbreviation and expected_abbreviation != actual_abbreviation:
                        raise ValueError(
                            f"{data_dir / CURATED_SCOPE_FILE} 的 {curated.scope_id!r} 简称校验失败："
                            f"期望 {curated.match_abbreviation!r}，榜单为 {abbreviation!r}"
                        )
                records.append(
                    VenueRecord(
                        row_id=len(records),
                        dataset=dataset,
                        source=normalize_space(row.get("source")),
                        source_file=normalize_space(row.get("source_file")),
                        version_year=version_year,
                        record_type=record_type,
                        name=name,
                        abbreviation=abbreviation,
                        issn=normalize_space(row.get("issn")),
                        eissn=normalize_space(row.get("eissn")),
                        area=normalize_space(row.get("area")),
                        area_en=normalize_space(row.get("area_en")),
                        level=normalize_space(row.get("level")),
                        taxonomy_scope=normalize_space(row.get("收稿方向")),
                        official_scope=normalize_space(row.get("收稿方向_官网摘取")),
                        official_scope_url=normalize_space(row.get("收稿方向_来源URL")),
                        official_scope_status=normalize_space(row.get("收稿方向_状态")),
                        official_scope_confidence=normalize_space(row.get("收稿方向_置信度")),
                        curated_scope_id=curated.scope_id if curated else "",
                        curated_scope=curated.scope_summary if curated else "",
                        curated_topics_zh=curated.keywords_zh if curated else "",
                        curated_topics_en=curated.keywords_en if curated else "",
                        curated_topic_tags=curated.topic_tags if curated else "",
                        curated_article_types=curated.article_types if curated else "",
                        curated_accepts_original_research=curated.accepts_original_research if curated else "",
                        curated_submission_mode=curated.submission_mode if curated else "",
                        curated_scope_context=curated.scope_context if curated else "",
                        curated_scope_year=curated.scope_year if curated else "",
                        curated_out_of_scope=curated.out_of_scope if curated else "",
                        curated_scope_basis=curated.source_type if curated else "",
                        curated_scope_status=curated.review_status if curated else "",
                        curated_secondary_source_urls=curated.secondary_source_urls if curated else "",
                        curated_target_status=curated.target_status if curated else "",
                        top=normalize_space(row.get("top")),
                        impact_factor=normalize_space(row.get("impact_factor")),
                    )
                )
    unmatched = sorted(set(curated_scopes) - matched_curated_keys)
    if unmatched:
        scope_ids = [curated_scopes[key].scope_id for key in unmatched]
        raise ValueError(
            f"{data_dir / CURATED_SCOPE_FILE} 有未命中榜单记录的审核范围："
            + ", ".join(scope_ids[:8])
        )
    if matched_curated_rows:
        row_group: dict[int, int] = {}
        entity_groups = group_records(records)
        for group_number, group in enumerate(entity_groups):
            for record in group:
                row_group[record.row_id] = group_number
            active_scope_ids = {
                record.curated_scope_id for record in group if record.curated_scope_id
            }
            if len(active_scope_ids) > 1:
                raise ValueError(
                    f"{data_dir / CURATED_SCOPE_FILE} 为同一投稿实体启用了多条审核范围："
                    + ", ".join(sorted(active_scope_ids))
                )
        for key, row_ids in matched_curated_rows.items():
            entity_groups = {row_group[row_id] for row_id in row_ids}
            if len(entity_groups) > 1:
                scope = curated_scopes[key]
                raise ValueError(
                    f"{data_dir / CURATED_SCOPE_FILE} 的 {scope.scope_id!r} 锚点命中了"
                    f" {len(entity_groups)} 个不同投稿实体；当前 schema 无法唯一识别，"
                    "请暂停启用该记录并先完善锚点字段"
                )
    return records


def group_records(records: Sequence[VenueRecord]) -> list[list[VenueRecord]]:
    """保守聚合同一投稿目标，显式处理已核实的跨年代期刊 lineage。"""

    dsu = DisjointSet(len(records))
    issn_owner: dict[str, int] = {}
    journal_lineage_owner: dict[str, int] = {}
    conference_name_owner: dict[str, int] = {}
    conference_abbreviations: dict[str, list[int]] = defaultdict(list)

    for index, record in enumerate(records):
        if record.record_type == "journal":
            for token in {valid_issn_token(record.issn), valid_issn_token(record.eissn)} - {""}:
                if token in issn_owner:
                    dsu.union(index, issn_owner[token])
                else:
                    issn_owner[token] = index
            lineage_name = journal_lineage_name(record.name)
            if lineage_name in JOURNAL_DISPLAY_NAMES:
                if lineage_name in journal_lineage_owner:
                    dsu.union(index, journal_lineage_owner[lineage_name])
                else:
                    journal_lineage_owner[lineage_name] = index
        elif record.record_type == "conference":
            name = conference_identity_name(record.name)
            if name and name in conference_name_owner:
                dsu.union(index, conference_name_owner[name])
            elif name:
                conference_name_owner[name] = index
            abbreviation = re.sub(r"[^a-z0-9]+", "", record.abbreviation.casefold())
            if abbreviation:
                conference_abbreviations[abbreviation].append(index)

    # 仅合并“每个榜单中简称唯一、且全名高度相似”的跨榜单会议。
    # FSE、SEC 等已知简称碰撞会因同一榜单出现多次而自动被排除。
    for indices in conference_abbreviations.values():
        if len(indices) < 2:
            continue
        dataset_counts = Counter(records[index].dataset for index in indices)
        if any(count > 1 for count in dataset_counts.values()):
            continue
        names = [normalize_name(records[index].name) for index in indices]
        similarities = [
            SequenceMatcher(None, names[left], names[right]).ratio()
            for left in range(len(names))
            for right in range(left)
        ]
        if similarities and min(similarities) >= 0.70:
            for index in indices[1:]:
                dsu.union(indices[0], index)

    identified_name_roots: dict[str, set[int]] = defaultdict(set)
    journal_without_ids: list[int] = []
    for index, record in enumerate(records):
        if record.record_type != "journal":
            continue
        tokens = {valid_issn_token(record.issn), valid_issn_token(record.eissn)} - {""}
        name = journal_identity_name(record.name)
        if tokens and name:
            identified_name_roots[name].add(dsu.find(index))
        elif not tokens:
            journal_without_ids.append(index)

    missing_name_owner: dict[str, int] = {}
    for index in journal_without_ids:
        name = journal_identity_name(records[index].name)
        if not name:
            continue
        possible_roots = identified_name_roots.get(name, set())
        if len(possible_roots) == 1:
            dsu.union(index, next(iter(possible_roots)))
        elif not possible_roots:
            if name in missing_name_owner:
                dsu.union(index, missing_name_owner[name])
            else:
                missing_name_owner[name] = index

    groups: dict[int, list[VenueRecord]] = defaultdict(list)
    for index, record in enumerate(records):
        groups[dsu.find(index)].append(record)
    return list(groups.values())


def build_candidates(
    records: Sequence[VenueRecord],
    targets: Sequence[TargetSpec],
    *,
    record_type: str = "all",
    area_filters: Sequence[str] = (),
    scope_filters: Sequence[str] = (),
    reviewed_scope_only: bool = False,
    include_inactive: bool = False,
) -> list[VenueCandidate]:
    return build_candidates_from_groups(
        group_records(records),
        targets,
        record_type=record_type,
        area_filters=area_filters,
        scope_filters=scope_filters,
        reviewed_scope_only=reviewed_scope_only,
        include_inactive=include_inactive,
    )


def build_candidates_from_groups(
    groups: Sequence[Sequence[VenueRecord]],
    targets: Sequence[TargetSpec],
    *,
    record_type: str = "all",
    area_filters: Sequence[str] = (),
    scope_filters: Sequence[str] = (),
    reviewed_scope_only: bool = False,
    include_inactive: bool = False,
) -> list[VenueCandidate]:
    """Build candidates from entity groups that were computed in memory or indexed."""

    target_keys = {target.key for target in targets}
    normalized_areas = [
        normalize_search_text(value)
        for value in area_filters
        if normalize_search_text(value)
    ]
    normalized_scopes = [
        normalize_search_text(value)
        for value in scope_filters
        if normalize_search_text(value)
    ]
    candidates = []
    for group in groups:
        matched = [record for record in group if record.target_key in target_keys]
        if record_type != "all":
            matched = [record for record in matched if record.record_type == record_type]
        if normalized_areas:
            group_area = normalize_search_text(
                " ".join(
                    value
                    for record in matched
                    for value in (
                        record.area,
                        record.area_en,
                        record.taxonomy_scope,
                    )
                    if value
                )
            )
            if not any(_matches_search_filter(group_area, area) for area in normalized_areas):
                matched = []
        curated_text = normalize_search_text(
            " ".join(
                value
                for record in group
                if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
                for value in (
                    record.curated_scope,
                    record.curated_topics_zh,
                    record.curated_topics_en,
                    record.curated_topic_tags,
                )
                if value
            )
        )
        if reviewed_scope_only and not curated_text:
            matched = []
        if not include_inactive:
            target_statuses = {
                record.curated_target_status
                for record in group
                if record.curated_scope_status in CURATED_SCOPE_ACTIVE_STATUSES
            }
            if target_statuses & {"historical_merged", "family_non_actionable"}:
                matched = []
        if normalized_scopes and not any(
            _matches_search_filter(curated_text, scope) for scope in normalized_scopes
        ):
            matched = []
        if matched:
            candidates.append(VenueCandidate(records=list(group), matched_records=matched))
    return candidates


def tokenize(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: Counter[str] = Counter()
    for word in ASCII_WORD_RE.findall(normalized):
        if len(word) >= 2 and word not in ENGLISH_STOP_WORDS:
            tokens[word] += 1
    for sequence in CJK_SEQUENCE_RE.findall(normalized):
        if 3 <= len(sequence) <= 10 and sequence not in CJK_STOP_TOKENS:
            tokens[sequence] += 1
        for index in range(max(0, len(sequence) - 1)):
            token = sequence[index : index + 2]
            if token not in CJK_STOP_TOKENS:
                tokens[token] += 1
    return tokens


OUT_OF_SCOPE_GENERIC_TERMS = {
    "纯",
    "通常",
    "不匹配",
    "没有",
    "缺少",
    "不涉及",
    "论文",
    "稿件",
    "文章",
    "研究",
    "工作",
    "方法",
    "系统",
    "应用",
    "贡献",
    "一般",
    "通用",
    "普通",
    "only",
    "just",
    "pure",
    "without",
    "generic",
}


def _scope_signal_terms(text: str) -> set[str]:
    """提取负向范围中的可比对短语，兼容较长中文连续片段。"""

    normalized = unicodedata.normalize("NFKC", normalize_space(text)).casefold()
    terms = {
        token
        for token in tokenize(normalized)
        if token not in OUT_OF_SCOPE_GENERIC_TERMS
        and (len(token) >= 3 or bool(re.fullmatch(r"[a-z0-9+#.]{2,}", token)))
    }
    for sequence in CJK_SEQUENCE_RE.findall(normalized):
        if len(sequence) < 3:
            continue
        for width in range(3, min(8, len(sequence)) + 1):
            for start in range(len(sequence) - width + 1):
                term = sequence[start : start + width]
                if term not in OUT_OF_SCOPE_GENERIC_TERMS:
                    terms.add(term)
    return terms


def _out_of_scope_conflict_score(query: str, out_of_scope: Sequence[str]) -> float:
    """返回查询与审核排除边界的冲突强度，范围为 0–1。"""

    if not out_of_scope or not NEGATIVE_SCOPE_CUE_RE.search(query):
        return 0.0
    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    compact_query = re.sub(r"\s+", "", normalized_query)
    intervals: list[tuple[int, int]] = []
    for boundary in out_of_scope:
        for term in _scope_signal_terms(boundary):
            normalized_term = re.sub(r"\s+", "", term)
            if len(normalized_term) < 3 and not re.fullmatch(
                r"[a-z0-9+#.]{2,}", normalized_term
            ):
                continue
            start = compact_query.find(normalized_term)
            while start >= 0:
                intervals.append((start, start + len(normalized_term)))
                start = compact_query.find(normalized_term, start + 1)
    if not intervals:
        return 0.0
    # 选择覆盖最长文本的区间，避免中文滑动窗口把同一短语重复计数。
    covered = 0
    current_start, current_end = sorted(intervals, key=lambda item: (item[0], -item[1]))[0]
    for start, end in sorted(intervals, key=lambda item: (item[0], -item[1]))[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            covered += current_end - current_start
            current_start, current_end = start, end
    covered += current_end - current_start
    score = covered / max(1, len(compact_query))
    if re.search(r"(?:只把|仅把|only|just).{0,40}(?:平台|platform)", normalized_query, re.I):
        if any(
            re.search(r"(?:只把|仅把|only|just).{0,70}(?:平台|platform)", boundary, re.I)
            or re.search(r"(?:现成|普通).{0,40}(?:平台|platform)", boundary, re.I)
            for boundary in out_of_scope
        ):
            score = max(score, 1.0)
    return min(1.0, score)


def rank_candidates(
    candidates: Sequence[VenueCandidate],
    query: str,
    *,
    include_official_scope: bool = False,
    statistics: RankingStatistics | None = None,
    semantic_scores: Mapping[int, float] | None = None,
    semantic_weight: float = 6.0,
    semantic_min_similarity: float = 0.35,
    graph_scores: Mapping[int, float] | None = None,
    graph_paths: Mapping[int, Sequence[str]] | None = None,
    graph_weight: float = 4.0,
    lightrag_scores: Mapping[int, float] | None = None,
    lightrag_channels: Mapping[int, Sequence[str]] | None = None,
    lightrag_weight: float = 5.0,
    constraint_query: str | None = None,
    additional_query_concepts: Sequence[tuple[str, str]] = (),
) -> list[VenueCandidate]:
    query_tokens = tokenize(query)
    query_concepts = merge_query_concepts(query, additional_query_concepts)
    if not query_tokens:
        return []
    constraint_text = query if constraint_query is None else constraint_query

    field_weights = {
        "name": 2.5,
        "abbreviation": 4.0,
        "area": 3.5,
        "taxonomy_scope": 2.5,
        "curated_scope": 5.0,
        "curated_topics": 5.5,
        "official_scope": 1.0,
    }
    candidate_fields: list[dict[str, set[str]]] = []
    candidate_topic_tags: list[set[str]] = []
    document_frequency: Counter[str] = Counter(
        statistics.document_frequency if statistics else {}
    )
    concept_document_frequency: Counter[str] = Counter(
        statistics.concept_document_frequency if statistics else {}
    )
    for candidate in candidates:
        fields = {
            name: set(tokenize(value))
            for name, value in candidate.matching_document(include_official_scope).items()
        }
        candidate_fields.append(fields)
        topic_tags = set(candidate.curated_topic_tags)
        candidate_topic_tags.append(topic_tags)
        if statistics is None:
            document_tokens = set().union(*fields.values())
            document_frequency.update(document_tokens)
            concept_document_frequency.update(topic_tags)

    total_documents = max(
        1, statistics.total_documents if statistics else len(candidates)
    )
    reviewed_documents = max(
        1,
        statistics.reviewed_documents
        if statistics
        else sum(bool(tags) for tags in candidate_topic_tags),
    )
    if not math.isfinite(semantic_weight) or semantic_weight < 0:
        raise ValueError("semantic weight cannot be negative")
    if not math.isfinite(graph_weight) or graph_weight < 0:
        raise ValueError("graph weight cannot be negative")
    if not math.isfinite(lightrag_weight) or lightrag_weight < 0:
        raise ValueError("LightRAG weight cannot be negative")
    if not -1.0 <= semantic_min_similarity < 1.0:
        raise ValueError("semantic minimum similarity must be in [-1, 1)")
    semantic_scores = semantic_scores or {}
    graph_scores = graph_scores or {}
    graph_paths = graph_paths or {}
    lightrag_scores = lightrag_scores or {}
    lightrag_channels = lightrag_channels or {}
    ranked = []
    query_normalizer = math.sqrt(max(1, len(query_tokens) + len(query_concepts)))
    original_research_intent, review_article_intent = _article_intents(constraint_text)
    concept_labels = dict(query_concepts)
    for candidate, fields, topic_tags in zip(
        candidates, candidate_fields, candidate_topic_tags
    ):
        entity_id = min(record.row_id for record in candidate.records)
        semantic_similarity = semantic_scores.get(entity_id)
        candidate.semantic_similarity = semantic_similarity
        graph_relevance = graph_scores.get(entity_id)
        candidate.graph_relevance = graph_relevance
        candidate.graph_path = list(graph_paths.get(entity_id, ()))
        lightrag_relevance = lightrag_scores.get(entity_id)
        candidate.lightrag_relevance = lightrag_relevance
        candidate.lightrag_channels = list(lightrag_channels.get(entity_id, ()))
        if (
            original_research_intent
            and not review_article_intent
            and candidate.curated_accepts_original_research
            and set(candidate.curated_accepts_original_research) == {"no"}
        ):
            continue
        if (
            review_article_intent
            and not original_research_intent
            and candidate.curated_article_types
            and "survey_review" not in candidate.curated_article_types
            and not (
                "systematization_of_knowledge" in candidate.curated_article_types
                and SOK_ARTICLE_INTENT_RE.search(constraint_text)
            )
        ):
            continue
        negative_scope_score = _out_of_scope_conflict_score(
            constraint_text, candidate.curated_out_of_scope
        )
        if negative_scope_score >= 0.25:
            continue
        contributions: Counter[str] = Counter()
        concept_contributions: Counter[str] = Counter()
        field_contributions: Counter[str] = Counter()
        for token, query_count in query_tokens.items():
            inverse_frequency = (
                math.log((total_documents + 1) / (document_frequency[token] + 1)) + 1.0
            )
            field_matches = [
                (weight, field_name)
                for field_name, weight in field_weights.items()
                if token in fields[field_name]
            ]
            if not field_matches:
                continue
            field_score, matched_field = max(field_matches)
            contributions[token] = field_score * inverse_frequency * min(query_count, 2)
            field_contributions[matched_field] += contributions[token]
        for topic_tag, _display_name in query_concepts:
            if topic_tag not in topic_tags:
                continue
            inverse_frequency = (
                math.log(
                    (reviewed_documents + 1)
                    / (concept_document_frequency[topic_tag] + 1)
                )
                + 1.0
            )
            concept_contributions[topic_tag] = (
                QUERY_CONCEPT_WEIGHTS.get(topic_tag, 3.0) * inverse_frequency
            )
        if concept_contributions:
            field_contributions["curated_topic_tags"] += sum(concept_contributions.values())
        raw_score = (
            sum(contributions.values()) + sum(concept_contributions.values())
        ) / query_normalizer
        if semantic_similarity is not None:
            semantic_bonus = semantic_weight * max(
                0.0,
                (semantic_similarity - semantic_min_similarity)
                / (1.0 - semantic_min_similarity),
            )
            raw_score += semantic_bonus
        if graph_relevance is not None:
            raw_score += graph_weight * max(0.0, min(1.0, graph_relevance))
        if lightrag_relevance is not None:
            raw_score += lightrag_weight * max(
                0.0, min(1.0, lightrag_relevance)
            )
        matched_concept_tags = set(concept_contributions)
        broad_only = (
            len(query_concepts) >= 2
            and matched_concept_tags
            and matched_concept_tags <= GENERIC_QUERY_CONCEPTS
        )
        no_controlled_concept_overlap = bool(
            query_concepts and topic_tags and not matched_concept_tags
        )
        alignment_factor = 0.72 if broad_only else 0.65 if no_controlled_concept_overlap else 1.0
        if "journal_family" in candidate.curated_scope_contexts:
            alignment_factor *= 0.8
        if "invited_only" in candidate.curated_submission_modes:
            alignment_factor *= 0.25
        if "retired_merged" in candidate.curated_submission_modes:
            alignment_factor *= 0.25
        if negative_scope_score:
            alignment_factor *= max(0.1, 1.0 - negative_scope_score)
        candidate.score = raw_score * alignment_factor
        candidate.matched_terms = [term for term, _score in contributions.most_common(6)]
        candidate.matched_fields = [name for name, _score in field_contributions.most_common()]
        if semantic_similarity is not None:
            candidate.matched_fields.append("semantic_vector")
        if graph_relevance is not None:
            candidate.matched_fields.append("knowledge_graph_path")
        if lightrag_relevance is not None:
            candidate.matched_fields.append("lightrag_mix_recall")
        if broad_only:
            candidate.matched_fields.append("broad_concept_only")
        elif no_controlled_concept_overlap:
            candidate.matched_fields.append("no_controlled_concept_overlap")
        if "journal_family" in candidate.curated_scope_contexts:
            candidate.matched_fields.append("journal_family_scope")
        if "invited_only" in candidate.curated_submission_modes:
            candidate.matched_fields.append("invited_only_target")
        if "retired_merged" in candidate.curated_submission_modes:
            candidate.matched_fields.append("retired_merged_target")
        if negative_scope_score:
            candidate.matched_fields.append("negative_scope_conflict")
        candidate.matched_concepts = [
            concept_labels[topic_tag]
            for topic_tag, _score in concept_contributions.most_common()
        ]
        if candidate.score > 0:
            ranked.append(candidate)
    return sorted(ranked, key=lambda candidate: (-candidate.score, normalize_name(candidate.name)))


def rank_candidates_indexed(
    candidates: Sequence[VenueCandidate],
    query: str,
    search_index: object,
    *,
    include_official_scope: bool = False,
    lexical_limit: int | None = None,
    query_vector: Sequence[float] | None = None,
    vector_provider_fingerprint: str | None = None,
    vector_limit: int = 500,
    vector_min_similarity: float = 0.35,
    vector_weight: float = 6.0,
    approximate_vector_recall: bool = False,
    lightrag_entity_ids: Sequence[int] = (),
    lightrag_scores: Mapping[int, float] | None = None,
    lightrag_channels: Mapping[int, Sequence[str]] | None = None,
    lightrag_weight: float = 5.0,
    constraint_query: str | None = None,
    additional_query_concepts: Sequence[tuple[str, str]] = (),
) -> list[VenueCandidate]:
    """Use a persistent retrieval backend, then apply the explainable ranker."""

    if not candidates:
        return []
    candidate_by_entity = {
        min(record.row_id for record in candidate.records): candidate
        for candidate in candidates
    }
    query_tokens = list(tokenize(query))
    topic_tags = [
        topic_tag
        for topic_tag, _label in merge_query_concepts(query, additional_query_concepts)
    ]
    recall = search_index.recall(
        allowed_entity_ids=list(candidate_by_entity),
        query_tokens=query_tokens,
        topic_tags=topic_tags,
        include_official_scope=include_official_scope,
        lexical_limit=lexical_limit,
    )
    semantic_scores: Mapping[int, float] = {}
    graph_scores: Mapping[int, float] = getattr(recall, "graph_scores", {})
    graph_paths: Mapping[int, Sequence[str]] = getattr(recall, "graph_paths", {})
    vector_entity_ids: list[int] = []
    if query_vector is not None:
        if not vector_provider_fingerprint:
            raise ValueError("vector provider fingerprint is required")
        vector_recall = search_index.vector_recall(
            allowed_entity_ids=list(candidate_by_entity),
            query_vector=query_vector,
            provider_fingerprint=vector_provider_fingerprint,
            limit=vector_limit,
            min_similarity=vector_min_similarity,
            approximate=approximate_vector_recall,
        )
        semantic_scores = vector_recall.similarities
        vector_entity_ids = vector_recall.entity_ids
    recalled_entity_ids = list(recall.entity_ids)
    recalled_entity_set = set(recalled_entity_ids)
    recalled_entity_ids.extend(
        entity_id
        for entity_id in vector_entity_ids
        if entity_id not in recalled_entity_set
    )
    recalled_entity_set.update(recalled_entity_ids)
    recalled_entity_ids.extend(
        entity_id
        for entity_id in lightrag_entity_ids
        if entity_id not in recalled_entity_set
    )
    recalled_candidates = [
        candidate_by_entity[entity_id]
        for entity_id in recalled_entity_ids
        if entity_id in candidate_by_entity
    ]
    statistics = RankingStatistics(
        total_documents=recall.total_documents,
        reviewed_documents=recall.reviewed_documents,
        document_frequency=recall.document_frequency,
        concept_document_frequency=recall.concept_document_frequency,
    )
    ranked = rank_candidates(
        recalled_candidates,
        query,
        include_official_scope=include_official_scope,
        statistics=statistics,
        semantic_scores=semantic_scores,
        semantic_weight=vector_weight,
        semantic_min_similarity=vector_min_similarity,
        graph_scores=graph_scores,
        graph_paths=graph_paths,
        lightrag_scores=lightrag_scores,
        lightrag_channels=lightrag_channels,
        lightrag_weight=lightrag_weight,
        constraint_query=constraint_query,
        additional_query_concepts=additional_query_concepts,
    )
    lexical_ids = set(recall.lexical_scores)
    lexical_marker = (
        "property_graph_lexical_recall"
        if hasattr(recall, "graph_paths")
        else "fts5_bm25_recall"
    )
    for candidate in ranked:
        entity_id = min(record.row_id for record in candidate.records)
        if entity_id in lexical_ids:
            candidate.matched_fields.append(lexical_marker)
    return ranked


MULTICHANNEL_RECALL_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("combined", 12),
    ("semantic_vector", 8),
    ("lightrag_mix", 6),
    ("property_graph", 6),
    ("llm_area_route", 4),
    ("search_hint", 4),
)


def _allocate_multichannel_quotas(limit: int) -> dict[str, int]:
    """Scale the default 12/8/6/6/4/4 recall mix to an arbitrary pool size."""

    if limit < 1:
        return {name: 0 for name, _weight in MULTICHANNEL_RECALL_WEIGHTS}
    total_weight = sum(weight for _name, weight in MULTICHANNEL_RECALL_WEIGHTS)
    raw = [
        (name, limit * weight / total_weight)
        for name, weight in MULTICHANNEL_RECALL_WEIGHTS
    ]
    quotas = {name: int(value) for name, value in raw}
    remaining = limit - sum(quotas.values())
    for name, _value in sorted(
        raw,
        key=lambda item: (
            -(item[1] - int(item[1])),
            next(
                index
                for index, (channel, _weight) in enumerate(MULTICHANNEL_RECALL_WEIGHTS)
                if channel == item[0]
            ),
        ),
    )[:remaining]:
        quotas[name] += 1
    return quotas


def _allocate_adaptive_multichannel_quotas(
    query: str,
    channel_ids: Mapping[str, Sequence[int]],
    *,
    limit: int,
    matched_areas: Sequence[str] = (),
    ambiguity: float | None = None,
    cross_disciplinary: float | None = None,
) -> tuple[dict[str, int], dict[str, object]]:
    """Route recall seats from query demand and live channel availability.

    The router never consumes labels or result identities.  Channel health is
    measured against the fixed-budget baseline: a channel is fully healthy
    when it can supply its old reserved quota, while short or unavailable
    channels automatically release seats.  This makes the former
    ``12/8/6/6/4/4`` mix a reproducible ablation rather than a hidden fallback.
    """

    from .scope_rank import (
        AdaptiveBudgetAllocator,
        ChannelObservation,
        QueryProfile,
    )

    profile = QueryProfile.from_text(
        query,
        ambiguity=ambiguity,
        cross_disciplinary=cross_disciplinary,
    )
    distinct_areas = {
        normalize_search_text(value)
        for value in matched_areas
        if normalize_search_text(value)
    }
    if len(distinct_areas) > 1:
        # LLM-selected controlled taxonomy labels are a useful cross-domain
        # signal, but remain a soft route and can never become a hard filter.
        profile = QueryProfile.from_text(
            query,
            ambiguity=profile.ambiguity,
            cross_disciplinary=max(
                profile.cross_disciplinary,
                min(1.0, 0.20 * len(distinct_areas)),
            ),
            language=profile.language,
        )

    fixed_quotas = _allocate_multichannel_quotas(limit)
    observations = []
    for channel, _weight in MULTICHANNEL_RECALL_WEIGHTS:
        available_count = len(channel_ids.get(channel, ()))
        reference_quota = max(1, fixed_quotas[channel])
        observations.append(
            ChannelObservation(
                name=channel,
                confidence=min(1.0, available_count / reference_quota),
                coverage=min(1.0, available_count / max(1, limit)),
                # All inputs in this stage come from the same request/snapshot;
                # provenance age belongs in the learned fusion feature set.
                freshness=1.0,
                available=available_count > 0,
                capacity=available_count,
            )
        )
    allocation = AdaptiveBudgetAllocator(minimum_per_available=1).allocate(
        profile,
        observations,
        total_budget=limit,
    )
    return dict(allocation.quotas), {
        "mode": "scope_rank_adaptive",
        "profile": {
            "ambiguity": round(profile.ambiguity, 6),
            "cross_disciplinary": round(profile.cross_disciplinary, 6),
            "language": profile.language,
            "token_count": profile.token_count,
        },
        "channel_scores": {
            name: round(float(score), 8)
            for name, score in allocation.channel_scores.items()
        },
        "allocated": allocation.allocated,
        "unallocated": allocation.unallocated,
        "fixed_ablation_quotas": fixed_quotas,
    }


def build_multichannel_recall_pool(
    ranked_candidates: Sequence[VenueCandidate],
    candidate_catalog: Sequence[VenueCandidate],
    *,
    query: str = "",
    matched_areas: Sequence[str] = (),
    hinted_entity_ids: Sequence[int] = (),
    limit: int = 40,
    adaptive: bool = True,
    query_ambiguity: float | None = None,
    query_cross_disciplinary: float | None = None,
) -> tuple[list[int], dict[str, object]]:
    """Reserve candidates from independent recall channels before LLM reranking.

    A single fused score can otherwise crowd a whole channel out of a small
    Top-K.  This helper preserves the strong fused head while reserving seats
    for exact-vector, LightRAG, property-graph, LLM taxonomy-route and Search
    hint candidates.  Duplicate entities consume only one seat; unused seats
    are deterministically backfilled from every channel.
    """

    if limit < 1:
        return [], {"limit": limit, "quotas": {}, "channels": {}}

    def entity_id(candidate: VenueCandidate) -> int:
        return min(record.row_id for record in candidate.records)

    catalog_by_id = {entity_id(candidate): candidate for candidate in candidate_catalog}
    ranked_by_id = {entity_id(candidate): candidate for candidate in ranked_candidates}
    combined_ids = [entity_id(candidate) for candidate in ranked_candidates]

    semantic_ids = [
        entity_id(candidate)
        for candidate in sorted(
            (
                candidate
                for candidate in ranked_candidates
                if candidate.semantic_similarity is not None
            ),
            key=lambda candidate: (
                -(candidate.semantic_similarity or -1.0),
                -candidate.score,
                normalize_name(candidate.name),
            ),
        )
    ]
    lightrag_ids = [
        entity_id(candidate)
        for candidate in sorted(
            (
                candidate
                for candidate in ranked_candidates
                if candidate.lightrag_relevance is not None
            ),
            key=lambda candidate: (
                -(candidate.lightrag_relevance or -1.0),
                -candidate.score,
                normalize_name(candidate.name),
            ),
        )
    ]
    graph_ids = [
        entity_id(candidate)
        for candidate in ranked_candidates
        if any(
            marker in candidate.matched_fields
            for marker in (
                "property_graph_lexical_recall",
                "fts5_bm25_recall",
                "knowledge_graph_path",
            )
        )
    ]

    normalized_areas = {
        normalize_search_text(value) for value in matched_areas if normalize_search_text(value)
    }
    area_ids = [
        entity_id(candidate)
        for candidate in sorted(
            candidate_catalog,
            key=lambda candidate: (
                -ranked_by_id.get(entity_id(candidate), candidate).score,
                normalize_name(candidate.name),
            ),
        )
        if normalized_areas
        and any(
            normalize_search_text(value) in normalized_areas
            for record in candidate.records
            for value in (record.area, record.area_en)
            if normalize_search_text(value)
        )
    ]
    hint_ids = [
        int(value) for value in hinted_entity_ids if int(value) in catalog_by_id
    ]
    channel_ids: dict[str, list[int]] = {
        "combined": list(dict.fromkeys(combined_ids)),
        "semantic_vector": list(dict.fromkeys(semantic_ids)),
        "lightrag_mix": list(dict.fromkeys(lightrag_ids)),
        "property_graph": list(dict.fromkeys(graph_ids)),
        "llm_area_route": list(dict.fromkeys(area_ids)),
        "search_hint": list(dict.fromkeys(hint_ids)),
    }
    routing: dict[str, object]
    if adaptive and normalize_space(query):
        quotas, routing = _allocate_adaptive_multichannel_quotas(
            query,
            channel_ids,
            limit=limit,
            matched_areas=matched_areas,
            ambiguity=query_ambiguity,
            cross_disciplinary=query_cross_disciplinary,
        )
    else:
        quotas = _allocate_multichannel_quotas(limit)
        routing = {
            "mode": "fixed_ablation",
            "fixed_ablation_quotas": dict(quotas),
        }
    selected: list[int] = []
    selected_set: set[int] = set()
    selected_by_channel: dict[str, int] = {}

    def take(channel: str, count: int) -> None:
        added = 0
        for current_id in channel_ids[channel]:
            if current_id in selected_set:
                continue
            selected.append(current_id)
            selected_set.add(current_id)
            added += 1
            if len(selected) >= limit or added >= count:
                break
        selected_by_channel[channel] = selected_by_channel.get(channel, 0) + added

    for channel, _weight in MULTICHANNEL_RECALL_WEIGHTS:
        take(channel, quotas[channel])
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        # Round-robin backfill stops a long combined list from swallowing every
        # unused quota while still remaining deterministic.
        offsets = {name: 0 for name in channel_ids}
        while len(selected) < limit:
            progressed = False
            for channel, _weight in MULTICHANNEL_RECALL_WEIGHTS:
                values = channel_ids[channel]
                while offsets[channel] < len(values):
                    current_id = values[offsets[channel]]
                    offsets[channel] += 1
                    if current_id in selected_set:
                        continue
                    selected.append(current_id)
                    selected_set.add(current_id)
                    selected_by_channel[channel] = selected_by_channel.get(channel, 0) + 1
                    progressed = True
                    break
                if len(selected) >= limit:
                    break
            if not progressed:
                break

    channel_markers = {
        "llm_area_route": "llm_area_route_recall",
        "search_hint": "search_venue_hint_recall",
    }
    for current_id in selected:
        candidate = catalog_by_id.get(current_id)
        if candidate is None:
            continue
        if "multichannel_recall_pool" not in candidate.matched_fields:
            candidate.matched_fields.append("multichannel_recall_pool")
        for channel, marker in channel_markers.items():
            if current_id in channel_ids[channel] and marker not in candidate.matched_fields:
                candidate.matched_fields.append(marker)

    return selected, {
        "limit": limit,
        "selected": len(selected),
        "quotas": quotas,
        "routing": routing,
        "channels": {
            channel: {
                "available": len(values),
                "selected_from_channel": selected_by_channel.get(channel, 0),
                "represented_in_pool": sum(value in selected_set for value in values),
            }
            for channel, values in channel_ids.items()
        },
    }


def open_persistent_index(
    data_dir: Path,
    index_path: Path,
    *,
    force_rebuild: bool = False,
) -> tuple[object, bool, str]:
    """Open a fresh persistent index, rebuilding it atomically when necessary."""

    from .search_index import (
        VenueSearchIndex,
        build_index,
        inspect_index,
        source_digest,
    )

    digest = source_digest(data_dir)
    freshness = inspect_index(index_path, data_dir, expected_digest=digest)
    rebuilt = force_rebuild or not freshness.fresh
    if rebuilt:
        records = load_records(data_dir)
        groups = group_records(records)
        build_index(
            index_path,
            data_dir,
            records,
            groups,
            tokenize=tokenize,
            normalize_alias=normalize_name,
            display_name_for_group=lambda group: VenueCandidate(
                list(group), list(group)
            ).name,
            matching_document_for_group=lambda group: VenueCandidate(
                list(group), list(group)
            ).matching_document(True),
            expected_digest=digest,
        )
    return VenueSearchIndex(index_path), rebuilt, freshness.reason


def open_persistent_graph(
    data_dir: Path,
    graph_path: Path,
    *,
    force_rebuild: bool = False,
) -> tuple[object, bool, str]:
    """Open the graph-native store, rebuilding its atomic snapshot if stale."""

    from .graph_index import (
        VenueGraphIndex,
        build_graph,
        graph_source_digest,
        inspect_graph,
    )

    data_dir = data_dir.resolve()
    graph_path = graph_path.resolve()
    cache_key = (data_dir, graph_path)
    current_stamp = _graph_runtime_stamp(data_dir, graph_path)
    cached = _GRAPH_RUNTIME_CACHE.get(cache_key)
    if not force_rebuild and cached is not None and cached[0] == current_stamp:
        return cached[1], False, "runtime_cache"

    digest = graph_source_digest(data_dir)
    freshness = inspect_graph(graph_path, data_dir, expected_digest=digest)
    rebuilt = force_rebuild or not freshness.fresh
    if rebuilt:
        if os.environ.get("WPG_STRICT_GRAPH_READ_ONLY", "").strip() == "1":
            raise RuntimeError(
                "property graph is not fresh and strict read-only mode forbids rebuild: "
                f"{freshness.reason}"
            )
        records = load_records(data_dir)
        groups = group_records(records)
        build_graph(
            graph_path,
            data_dir,
            records,
            groups,
            tokenize=tokenize,
            normalize_alias=normalize_name,
            display_name_for_group=lambda group: VenueCandidate(
                list(group), list(group)
            ).name,
            matching_document_for_group=lambda group: VenueCandidate(
                list(group), list(group)
            ).matching_document(True),
            expected_digest=digest,
        )
    graph = VenueGraphIndex(graph_path)
    graph.validate()
    _GRAPH_RUNTIME_CACHE[cache_key] = (
        _graph_runtime_stamp(data_dir, graph_path),
        graph,
    )
    return graph, rebuilt, freshness.reason


def area_summary(
    records: Sequence[VenueRecord],
    targets: Sequence[TargetSpec],
    *,
    record_type: str = "all",
    include_inactive: bool = False,
) -> list[dict[str, object]]:
    target_keys = {target.key for target in targets}
    counts: Counter[tuple[str, str, str, str]] = Counter()
    for record in records:
        if record.target_key not in target_keys:
            continue
        if record_type != "all" and record.record_type != record_type:
            continue
        if (
            not include_inactive
            and record.curated_target_status
            in {"historical_merged", "family_non_actionable"}
        ):
            continue
        area = record.area or record.taxonomy_scope or "未分类"
        counts[(record.dataset, record.level, record.record_type, area)] += 1
    return [
        {
            "dataset": dataset,
            "level": level,
            "ranking": ranking_label(dataset, level),
            "record_type": current_type,
            "record_type_name": RECORD_TYPE_NAMES.get(current_type, current_type),
            "area": area,
            "count": count,
        }
        for (dataset, level, current_type, area), count in sorted(
            counts.items(),
            key=lambda item: (
                DATASET_ORDER.get(item[0][0], 99),
                item[0][1],
                item[0][2],
                -item[1],
                item[0][3],
            ),
        )
    ]


def sort_unranked_candidates(
    candidates: Sequence[VenueCandidate], targets: Sequence[TargetSpec]
) -> list[VenueCandidate]:
    target_order = {target.key: index for index, target in enumerate(targets)}

    def sort_key(candidate: VenueCandidate) -> tuple[int, str]:
        order = min(target_order.get(record.target_key, 999) for record in candidate.matched_records)
        return order, normalize_name(candidate.name)

    return sorted(candidates, key=sort_key)


def candidate_to_dict(candidate: VenueCandidate) -> dict[str, object]:
    return {
        "entity_id": min(record.row_id for record in candidate.records),
        "name": candidate.name,
        "abbreviation": candidate.abbreviation,
        "record_type": candidate.record_type,
        "record_type_name": RECORD_TYPE_NAMES.get(candidate.record_type, candidate.record_type),
        "matched_rankings": candidate.matched_ranking_labels,
        "all_rankings": candidate.all_ranking_labels,
        "areas": candidate.areas,
        "classification_scopes": candidate.taxonomy_scopes,
        "reviewed_scope_entries": candidate.curated_scope_entries,
        "reviewed_scopes": candidate.curated_scopes,
        "reviewed_scope_topics": candidate.curated_topics,
        "reviewed_scope_topic_tags": candidate.curated_topic_tags,
        "reviewed_scope_article_types": candidate.curated_article_types,
        "reviewed_scope_accepts_original_research": candidate.curated_accepts_original_research,
        "reviewed_scope_submission_modes": candidate.curated_submission_modes,
        "reviewed_scope_contexts": candidate.curated_scope_contexts,
        "reviewed_scope_years": candidate.curated_scope_years,
        "reviewed_scope_out_of_scope": candidate.curated_out_of_scope,
        "reviewed_scope_basis": candidate.curated_scope_bases,
        "reviewed_scope_secondary_sources": candidate.curated_secondary_source_urls,
        "reviewed_scope_target_status": candidate.curated_target_statuses,
        "official_scope_candidates": candidate.official_scope_candidates,
        "official_scope_notice": "官网范围为自动摘取候选，投稿前必须人工核验"
        if candidate.official_scope_candidates
        else "",
        "top": candidate.is_top,
        "impact_factors": candidate.impact_factors,
        "score": round(candidate.score, 4) if candidate.score else None,
        "semantic_similarity": round(candidate.semantic_similarity, 4)
        if candidate.semantic_similarity is not None
        else None,
        "graph_relevance": round(candidate.graph_relevance, 4)
        if candidate.graph_relevance is not None
        else None,
        "graph_path": candidate.graph_path,
        "lightrag_relevance": round(candidate.lightrag_relevance, 4)
        if candidate.lightrag_relevance is not None
        else None,
        "lightrag_channels": candidate.lightrag_channels,
        "api_relevance": round(candidate.api_relevance, 2)
        if candidate.api_relevance is not None
        else None,
        "api_confidence": candidate.api_confidence,
        "api_reason": candidate.api_reason,
        "api_evidence_urls": candidate.api_evidence_urls,
        "matched_terms": candidate.matched_terms,
        "matched_fields": candidate.matched_fields,
        "matched_concepts": candidate.matched_concepts,
    }


def render_text(
    candidates: Sequence[VenueCandidate],
    *,
    total: int,
    targets: Sequence[TargetSpec],
    query: str,
    area_filters: Sequence[str],
    scope_filters: Sequence[str],
    names_only: bool,
    max_scope_chars: int,
    empty_hint: str = "",
    catalog_warning: str = "",
) -> str:
    target_text = "、".join(target.label for target in targets)
    lines = [f"筛选：{target_text}", f"共找到 {total} 个投稿目标，当前显示 {len(candidates)} 个。"]
    if query:
        lines.append(f"主题：{normalize_space(query)}")
        if any(candidate.semantic_similarity is not None for candidate in candidates):
            lines.append("匹配分综合关键词与向量语义相关性，不是录用概率或权威排名。")
        else:
            lines.append("匹配分只表示当前数据中的主题词重合，不是录用概率或权威排名。")
    if area_filters:
        lines.append(f"分类过滤：{'、'.join(area_filters)}")
    if scope_filters:
        lines.append(f"审核范围过滤：{'、'.join(scope_filters)}")
    lines.append("请在投稿前自行核验最新官网、征稿范围、截止日期和投稿要求。")
    if catalog_warning:
        lines.append(catalog_warning)
    if empty_hint:
        lines.append(empty_hint)

    for index, candidate in enumerate(candidates, start=1):
        title = candidate.name
        if candidate.abbreviation and candidate.abbreviation.casefold() not in title.casefold():
            title = f"{candidate.abbreviation} — {title}"
        lines.append("")
        lines.append(f"{index}. {title}")
        lines.append(f"   类型：{RECORD_TYPE_NAMES.get(candidate.record_type, candidate.record_type)}")
        lines.append(f"   命中等级：{'；'.join(candidate.matched_ranking_labels)}")
        extra_rankings = [label for label in candidate.all_ranking_labels if label not in candidate.matched_ranking_labels]
        if extra_rankings:
            lines.append(f"   其他已知等级：{'；'.join(extra_rankings)}")
        if candidate.is_top:
            lines.append("   标记：中科院 Top")
        if query:
            matched_terms = "、".join(candidate.matched_terms) or "—"
            lines.append(f"   匹配分：{candidate.score:.3f}；命中词：{matched_terms}")
            if candidate.semantic_similarity is not None:
                lines.append(f"   语义相似度：{candidate.semantic_similarity:.3f}")
            if candidate.graph_relevance is not None:
                lines.append(f"   图谱关联度：{candidate.graph_relevance:.3f}")
                if candidate.graph_path:
                    lines.append(f"   图谱路径：{' → '.join(candidate.graph_path)}")
            if candidate.lightrag_relevance is not None:
                lines.append(
                    f"   LightRAG mix 关联度：{candidate.lightrag_relevance:.3f}"
                )
            if candidate.api_relevance is not None:
                lines.append(
                    f"   API 主题适配：{candidate.api_relevance:.1f}/100"
                    + (f"（{candidate.api_confidence}）" if candidate.api_confidence else "")
                )
                if candidate.api_reason:
                    lines.append(f"   API 重排理由：{candidate.api_reason}")
                if candidate.api_evidence_urls:
                    lines.append(f"   API 证据：{candidate.api_evidence_urls[0]}")
            if candidate.matched_concepts:
                lines.append(f"   识别方向：{'、'.join(candidate.matched_concepts)}")
        if names_only:
            continue
        if candidate.curated_scopes:
            scope_metadata = [
                *candidate.curated_scope_years,
                *(
                    SCOPE_CONTEXT_NAMES.get(value, value)
                    for value in candidate.curated_scope_contexts
                ),
            ]
            scope_label = "已审核细粒度范围"
            if scope_metadata:
                scope_label += f"（{'，'.join(scope_metadata)}）"
            lines.append(
                f"   {scope_label}：{_truncate(candidate.curated_scopes[0], max_scope_chars)}"
            )
            if candidate.curated_topics:
                lines.append(f"   范围关键词：{_truncate('、'.join(candidate.curated_topics), max_scope_chars)}")
            if candidate.curated_article_types:
                lines.append(
                    "   接收文章："
                    + "、".join(
                        ARTICLE_TYPE_NAMES.get(value, value)
                        for value in candidate.curated_article_types
                    )
                )
            if candidate.curated_submission_modes:
                lines.append(
                    "   投稿方式："
                    + "、".join(
                        SUBMISSION_MODE_NAMES.get(value, value)
                        for value in candidate.curated_submission_modes
                    )
                )
            if candidate.curated_target_statuses:
                lines.append(
                    "   目标状态："
                    + "、".join(
                        TARGET_STATUS_NAMES.get(value, value)
                        for value in candidate.curated_target_statuses
                    )
                )
            if candidate.curated_out_of_scope:
                lines.append(f"   明确不收/受限：{_truncate(candidate.curated_out_of_scope[0], max_scope_chars)}")
        scope = "；".join(candidate.taxonomy_scopes or candidate.areas)
        if scope:
            lines.append(f"   分类范围：{_truncate(scope, max_scope_chars)}")
        if candidate.official_scope_candidates:
            lines.append(
                "   官网范围候选（自动摘取，待核验）："
                + _truncate(candidate.official_scope_candidates[0], max_scope_chars)
            )
    return "\n".join(lines) + "\n"


def render_areas_text(rows: Sequence[dict[str, object]], targets: Sequence[TargetSpec]) -> str:
    lines = [f"筛选：{'、'.join(target.label for target in targets)}", f"共 {len(rows)} 个等级/类型/分类组合。"]
    previous = None
    for row in rows:
        section = (row["ranking"], row["record_type_name"])
        if section != previous:
            lines.extend(["", f"{section[0]} · {section[1]}"])
            previous = section
        lines.append(f"- {row['area']}：{row['count']} 条")
    return "\n".join(lines) + "\n"


def write_csv_output(candidates: Sequence[VenueCandidate]) -> None:
    fields = [
        "name",
        "abbreviation",
        "record_type",
        "matched_rankings",
        "all_rankings",
        "classification_scopes",
        "reviewed_scopes",
        "reviewed_scope_topics",
        "reviewed_scope_topic_tags",
        "reviewed_scope_article_types",
        "reviewed_scope_accepts_original_research",
        "reviewed_scope_submission_modes",
        "reviewed_scope_contexts",
        "reviewed_scope_years",
        "reviewed_scope_out_of_scope",
        "reviewed_scope_basis",
        "reviewed_scope_secondary_sources",
        "reviewed_scope_target_status",
        "official_scope_candidates",
        "official_scope_notice",
        "top",
        "impact_factors",
        "score",
        "semantic_similarity",
        "graph_relevance",
        "graph_path",
        "lightrag_relevance",
        "lightrag_channels",
        "api_relevance",
        "api_confidence",
        "api_reason",
        "api_evidence_urls",
        "matched_terms",
        "matched_fields",
        "matched_concepts",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fields)
    writer.writeheader()
    for candidate in candidates:
        data = candidate_to_dict(candidate)
        writer.writerow(
            {
                field: "；".join(str(item) for item in data[field])
                if isinstance(data.get(field), list)
                else data.get(field, "")
                for field in fields
            }
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 CCF、TH-CPL、中科院或 JCR 等级筛选投稿目标，并按论文/项目主题排序。"
    )
    parser.add_argument(
        "-t",
        "--target",
        action="append",
        required=True,
        help="目标等级，可重复或逗号分隔，例如 CCF-A、THCPL-A、中科院1区、JCR-Q1。",
    )
    parser.add_argument("-q", "--query", default="", help="论文题目、摘要、关键词或项目描述；不填则仅列清单。")
    parser.add_argument("--query-file", type=Path, help="从 UTF-8 文本文件读取论文摘要或项目描述。")
    parser.add_argument(
        "--record-type",
        choices=["all", "journal", "conference"],
        default="all",
        help="只看期刊、会议或全部。",
    )
    parser.add_argument("--area", action="append", default=[], help="按分类范围进一步过滤，可重复。")
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help="只在已审核细粒度范围和关键词中搜索，可重复；多个条件为‘或’。",
    )
    parser.add_argument(
        "--reviewed-scope-only",
        action="store_true",
        help="只列出已有审核细粒度范围的投稿目标。",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="包含历史合并实体和不可直接投稿的刊系占位项（默认排除）。",
    )
    parser.add_argument("--areas", action="store_true", help="汇总所选等级包含的分类范围，而不是列出具体会刊。")
    parser.add_argument("-n", "--limit", type=int, default=30, help="最多显示多少个结果，默认 30。")
    parser.add_argument("--all", action="store_true", dest="show_all", help="显示所有结果。")
    parser.add_argument("--names-only", action="store_true", help="只显示名称、类型和等级。")
    parser.add_argument(
        "--match-official-scope",
        action="store_true",
        help="将尚未经人工复核的官网自动摘取范围用于匹配（默认不使用）。",
    )
    parser.add_argument(
        "--api-assisted-search",
        action="store_true",
        default=True,
        help="兼容选项；主题检索已强制使用 LLM 和 Search API。",
    )
    parser.add_argument(
        "--api-config",
        type=Path,
        default=None,
        help="含 llm 和 search 配置节的 JSON；默认查找 api.json/llmapi.json。",
    )
    parser.add_argument(
        "--api-cache-dir",
        type=Path,
        default=_environment_path(API_CACHE_DIR_ENV),
        help=(
            "LLM 查询规划、重排和 Search API 结果缓存目录；"
            f"也可通过 {API_CACHE_DIR_ENV} 绑定。"
        ),
    )
    parser.add_argument(
        "--api-candidate-limit",
        type=int,
        default=40,
        help="交给 LLM 证据重排的候选数，默认 40。",
    )
    parser.add_argument(
        "--fixed-recall-budget",
        action="store_true",
        help="论文消融选项：使用旧的 12/8/6/6/4/4 固定通道配额。",
    )
    parser.add_argument(
        "--api-search-query-limit",
        type=int,
        default=3,
        help="最多执行的 Search API 查询数，默认 3。",
    )
    parser.add_argument(
        "--api-search-results",
        type=int,
        default=8,
        help="每条 Search API 查询保留的结果数，默认 8。",
    )
    parser.add_argument(
        "--api-timeout",
        type=int,
        default=20,
        help="Search API 单次请求超时秒数，默认 20。",
    )
    parser.add_argument(
        "--api-rerank-weight",
        type=float,
        default=1.0,
        help="LLM 重排在倒数排名融合中的权重，默认 1.0。",
    )
    parser.add_argument(
        "--no-api-explanations",
        action="store_true",
        help="跳过最终前十解释调用；不改变候选评分、融合排序或命中指标。",
    )
    parser.add_argument("--format", choices=["text", "json", "csv"], default="text")
    parser.add_argument("--max-scope-chars", type=int, default=240, help="文本输出中每段范围的最大字符数。")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="数据目录。")
    parser.add_argument(
        "--graph",
        type=Path,
        default=None,
        help="属性图谱快照；默认位于数据目录的 venue_graph.json.gz。",
    )
    parser.add_argument(
        "--rebuild-graph",
        action="store_true",
        help="查询前强制重建属性图谱。",
    )
    parser.add_argument(
        "--no-graph",
        action="store_true",
        help="跳过属性图谱，直接加载 CSV（仅用于诊断和结果对照）。",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=None,
        help="兼容选项：显式使用旧 SQLite 索引；默认不再使用 SQLite。",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="兼容别名：强制重建当前持久层；新代码请用 --rebuild-graph。",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="兼容别名：等同 --no-graph。",
    )
    parser.add_argument(
        "--candidate-pool",
        type=int,
        default=0,
        help="主题查询的词法候选池大小；默认 0，不限制候选。",
    )
    parser.add_argument(
        "--vector-search",
        action="store_true",
        default=True,
        help="兼容选项；主题检索已强制使用精确向量语义召回。",
    )
    parser.add_argument(
        "--embedding-config",
        type=Path,
        default=None,
        help="含独立 embedding 配置节的 JSON 文件。",
    )
    parser.add_argument(
        "--embedding-cache",
        type=Path,
        default=None,
        help=(
            "已废弃的单缓存兼容选项；主题检索必须分别使用 "
            "--query-embedding-cache 和 --lightrag-embedding-cache。"
        ),
    )
    parser.add_argument(
        "--query-embedding-cache",
        type=Path,
        default=_environment_path(QUERY_EMBEDDING_CACHE_ENV),
        help=(
            "仅写入查询向量的 gzip/JSON 缓存；"
            f"也可通过 {QUERY_EMBEDDING_CACHE_ENV} 绑定。"
        ),
    )
    parser.add_argument(
        "--lightrag-embedding-cache",
        type=Path,
        default=_environment_path(LIGHTRAG_EMBEDDING_CACHE_ENV),
        help=(
            "仅写入 LightRAG 查询向量的 gzip/JSON 缓存；"
            f"也可通过 {LIGHTRAG_EMBEDDING_CACHE_ENV} 绑定。"
        ),
    )
    parser.add_argument(
        "--vector-limit",
        type=int,
        default=500,
        help="语义召回候选数，默认 500。",
    )
    parser.add_argument(
        "--approximate-vector-search",
        action="store_true",
        help="使用符号位近似向量候选；默认执行全量精确余弦扫描。",
    )
    parser.add_argument(
        "--vector-min-similarity",
        type=float,
        default=0.35,
        help="向量召回的最低余弦相似度，默认 0.35。",
    )
    parser.add_argument(
        "--vector-weight",
        type=float,
        default=6.0,
        help="语义相关性在混合排序中的权重，默认 6.0。",
    )
    parser.add_argument(
        "--lightrag-working-dir",
        type=Path,
        default=None,
        help="LightRAG 存储目录；默认 data/lightrag_storage。",
    )
    parser.add_argument(
        "--lightrag-top-k",
        type=int,
        default=200,
        help="LightRAG mix 图实体/关系召回数，默认 200。",
    )
    parser.add_argument(
        "--lightrag-chunk-top-k",
        type=int,
        default=200,
        help="LightRAG mix 向量文本块召回数，默认 200。",
    )
    parser.add_argument(
        "--lightrag-weight",
        type=float,
        default=5.0,
        help="LightRAG mix 召回信号在混合排序中的权重，默认 5.0。",
    )
    return parser


RetrievalEventCallback = Callable[[Mapping[str, object]], None]


def _emit_retrieval_event(
    callback: RetrievalEventCallback | None,
    event_type: str,
    **payload: object,
) -> None:
    """Report optional web-stream events without changing CLI output semantics."""

    if callback is None:
        return
    try:
        callback({"type": event_type, **payload})
    except Exception:
        # A disconnected browser must not interrupt the mandatory retrieval path.
        return


def main(
    argv: Sequence[str] | None = None,
    *,
    event_callback: RetrievalEventCallback | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    topic_query_requested = bool(args.query or args.query_file)
    if topic_query_requested and args.embedding_cache is not None:
        parser.error(
            "--embedding-cache 不能用于主题检索；请显式指定两个不同的 "
            "--query-embedding-cache 和 --lightrag-embedding-cache"
        )
    from .embeddings import (
        default_graph_embedding_cache_path,
        default_query_embedding_cache_path,
    )

    effective_query_embedding_cache = (
        args.query_embedding_cache
        or default_query_embedding_cache_path(args.data_dir)
    ).resolve()
    effective_lightrag_embedding_cache = (
        args.lightrag_embedding_cache
        or default_graph_embedding_cache_path(args.data_dir)
    ).resolve()
    effective_api_cache_dir = (
        args.api_cache_dir or args.data_dir / ".query_api_cache"
    ).resolve()
    if effective_query_embedding_cache == effective_lightrag_embedding_cache:
        parser.error(
            "--query-embedding-cache 和 --lightrag-embedding-cache "
            "必须指向不同文件"
        )
    args.query_embedding_cache = effective_query_embedding_cache
    args.lightrag_embedding_cache = effective_lightrag_embedding_cache
    args.api_cache_dir = effective_api_cache_dir
    if any(
        cache_path == effective_api_cache_dir
        or cache_path.is_relative_to(effective_api_cache_dir)
        or effective_api_cache_dir.is_relative_to(cache_path)
        for cache_path in (
            effective_query_embedding_cache,
            effective_lightrag_embedding_cache,
        )
    ):
        parser.error(
            "--api-cache-dir 必须与两个 embedding 缓存文件使用互不嵌套的路径"
        )
    if (
        args.api_config is not None
        and args.embedding_config is not None
        and args.api_config.resolve() != args.embedding_config.resolve()
    ):
        parser.error(
            "强制检索链路要求 LLM、embedding 和 search 使用同一配置文件"
        )
    forced_config_path = args.api_config or args.embedding_config
    if any(not normalize_search_text(value) for value in args.area):
        parser.error("--area 必须包含有效的分类文字")
    if any(not normalize_search_text(value) for value in args.scope):
        parser.error("--scope 必须包含有效的方向文字")
    if args.query and args.query_file:
        parser.error("--query 和 --query-file 不能同时使用")
    if args.areas and (args.query or args.query_file):
        parser.error("--areas 只用于分类汇总，不能同时提供主题查询")
    if args.areas and (args.scope or args.reviewed_scope_only):
        parser.error("--areas 只汇总榜单基础分类，不能同时使用 --scope 或 --reviewed-scope-only")
    if args.areas and args.area:
        parser.error("--areas 不能与 --area 同时使用")
    if args.areas and args.match_official_scope:
        parser.error("--areas 不能与 --match-official-scope 同时使用")
    if args.limit < 1 and not args.show_all:
        parser.error("--limit 必须大于 0；显示全部请使用 --all")
    if args.max_scope_chars < 40:
        parser.error("--max-scope-chars 不能小于 40")
    if args.candidate_pool < 0:
        parser.error("--candidate-pool 不能小于 0")
    if args.vector_limit < 1:
        parser.error("--vector-limit 必须大于 0")
    if not -1.0 <= args.vector_min_similarity < 1.0:
        parser.error("--vector-min-similarity 必须位于 [-1, 1) 区间")
    if not math.isfinite(args.vector_weight) or args.vector_weight < 0:
        parser.error("--vector-weight 不能小于 0")
    if args.api_candidate_limit < 5:
        parser.error("--api-candidate-limit 不能小于 5")
    if not 1 <= args.api_search_query_limit <= 5:
        parser.error("--api-search-query-limit 必须位于 [1, 5]")
    if not 1 <= args.api_search_results <= 20:
        parser.error("--api-search-results 必须位于 [1, 20]")
    if args.api_timeout < 1:
        parser.error("--api-timeout 必须大于 0")
    if not math.isfinite(args.api_rerank_weight) or args.api_rerank_weight < 0:
        parser.error("--api-rerank-weight 不能小于 0")
    if args.lightrag_top_k < 1 or args.lightrag_chunk_top_k < 1:
        parser.error("LightRAG top-k 必须大于 0")
    if not math.isfinite(args.lightrag_weight) or args.lightrag_weight < 0:
        parser.error("--lightrag-weight 不能小于 0")
    if args.graph is not None and args.index is not None:
        parser.error("--graph 不能与兼容模式 --index 同时使用")
    skip_persistent = args.no_graph or args.no_index
    force_rebuild = args.rebuild_graph or args.rebuild_index
    if skip_persistent and force_rebuild:
        parser.error("跳过图谱时不能同时强制重建")
    if skip_persistent and (args.graph is not None or args.index is not None):
        parser.error("跳过图谱时不能指定持久层路径")
    if topic_query_requested and skip_persistent:
        parser.error("主题检索强制使用图谱、LightRAG 和向量，不能跳过图谱")
    if topic_query_requested and args.index is not None:
        parser.error("主题检索不允许使用旧 SQLite 兼容层")

    scope_catalog_available = (args.data_dir / CURATED_SCOPE_FILE).exists()
    if (args.scope or args.reviewed_scope_only) and not scope_catalog_available:
        parser.error(
            f"显式细粒度范围查询需要数据文件：{args.data_dir / CURATED_SCOPE_FILE}"
        )

    try:
        targets = parse_targets(args.target)
    except ValueError as exc:
        parser.error(str(exc))

    query = args.query
    if args.query_file:
        try:
            query = args.query_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            parser.error(f"无法按 UTF-8 文本读取查询文件：{exc}")
    query = normalize_space(query)
    if query and not tokenize(query):
        parser.error("主题查询没有有效文字；请提供论文题目、摘要或关键词")
    # 列表/分类汇总不是主题检索；只有主题查询强制运行全部增强层。
    args.vector_search = bool(query)
    args.api_assisted_search = bool(query)

    search_index = None
    indexed_groups: list[list[VenueRecord]] | None = None
    index_warning = ""
    index_used = False
    backend_name = "csv_memory"
    using_graph_backend = False
    if not skip_persistent:
        if args.index is not None:
            persistent_path = args.index
            open_backend = open_persistent_index
            backend_name = "legacy_sqlite_fts5"
        else:
            from .graph_index import default_graph_path

            persistent_path = args.graph or default_graph_path(args.data_dir)
            open_backend = open_persistent_graph
            backend_name = "property_graph"
            using_graph_backend = True
        try:
            search_index, rebuilt, rebuild_reason = open_backend(
                args.data_dir,
                persistent_path,
                force_rebuild=force_rebuild,
            )
            raw_groups = search_index.load_groups_for_targets(
                [target.key for target in targets]
            )
            indexed_groups = [
                [VenueRecord(**record) for record in group]
                for _entity_id, group in raw_groups
            ]
            index_used = True
            if rebuilt:
                reason = "forced" if force_rebuild else rebuild_reason
                print(
                    f"持久检索层已重建：{persistent_path}（原因：{reason}）",
                    file=sys.stderr,
                )
        except (FileNotFoundError, OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
            if search_index is not None:
                search_index.close()
                search_index = None
            if args.vector_search:
                parser.error(f"向量语义召回需要可用的持久化索引：{exc}")
            if force_rebuild:
                parser.error(f"无法重建持久检索层：{exc}")
            index_warning = f"警告：持久检索层不可用，已回退到 CSV：{exc}"
            print(index_warning, file=sys.stderr)

    if indexed_groups is None:
        try:
            records = load_records(args.data_dir)
        except (ValueError, FileNotFoundError) as exc:
            parser.error(str(exc))
        indexed_groups = group_records(records)

    if args.areas:
        records_for_summary = [
            record for group in indexed_groups for record in group
        ]
        rows = area_summary(
            records_for_summary,
            targets,
            record_type=args.record_type,
            include_inactive=args.include_inactive,
        )
        if search_index is not None:
            search_index.close()
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "targets": [target.label for target in targets],
                        "search_backend": backend_name if index_used else "csv_memory",
                        "areas": rows,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.format == "csv":
            writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]) if rows else ["ranking", "area", "count"])
            writer.writeheader()
            writer.writerows(rows)
        else:
            sys.stdout.write(render_areas_text(rows, targets))
        return 0

    candidates = build_candidates_from_groups(
        indexed_groups,
        targets,
        record_type=args.record_type,
        area_filters=args.area,
        scope_filters=args.scope,
        reviewed_scope_only=args.reviewed_scope_only,
        include_inactive=args.include_inactive,
    )
    candidate_catalog = list(candidates)
    selected_target_keys = {target.key for target in targets}
    available_area_labels = sorted(
        {
            normalize_space(record.area)
            for group in indexed_groups
            for record in group
            if record.target_key in selected_target_keys
            and normalize_space(record.area)
        },
        key=lambda value: (value.casefold(), value),
    ) if query else []
    retrieval_query = query
    semantic_query = query
    additional_query_concepts: list[tuple[str, str]] = []
    effective_match_official_scope = args.match_official_scope
    api_assistant = None
    api_config: dict[str, object] = {}
    api_plan = None
    api_warning = ""
    api_info: dict[str, object] = {"enabled": False}
    api_cache_dir = args.api_cache_dir
    if args.api_assisted_search:
        from .api_assistant import (
            ApiAssistantError,
            OpenAICompatibleQueryAssistant,
            load_api_assistant_config,
        )

        try:
            api_config = load_api_assistant_config(forced_config_path)
            search_section = api_config.get("search")
            if not isinstance(search_section, dict) or not str(
                search_section.get("provider") or ""
            ).strip():
                raise ApiAssistantError(
                    "强制检索需要显式的 search.provider 配置"
                )
            api_assistant = OpenAICompatibleQueryAssistant(api_config, api_cache_dir)
        except ApiAssistantError as exc:
            if search_index is not None:
                search_index.close()
            parser.error(f"API 辅助检索配置无效：{exc}")
        api_info = {
            "enabled": True,
            "status": "planning",
            "model": api_assistant.model,
            "search_provider": str(
                (api_config.get("search") or {}).get("provider") or "duckduckgo"
            ),
            "candidate_limit": args.api_candidate_limit,
            "rerank_weight": args.api_rerank_weight,
        }
        _emit_retrieval_event(
            event_callback,
            "progress",
            stage="llm",
            status="running",
        )
        try:
            api_plan = api_assistant.plan_query(
                query,
                QUERY_CONCEPT_LABELS,
                area_filters=args.area,
                available_areas=available_area_labels,
            )
            retrieval_query = api_plan.retrieval_query(query)
            semantic_query = api_plan.semantic_query(query)
            additional_query_concepts = [
                (topic_tag, QUERY_CONCEPT_LABELS[topic_tag])
                for topic_tag in api_plan.topic_tags
            ]
            effective_match_official_scope = True
            if args.area:
                normalized_selected_areas = {
                    normalize_search_text(value) for value in args.area
                }
                exact_source_areas = [
                    value
                    for value in available_area_labels
                    if normalize_search_text(value) in normalized_selected_areas
                ]
                resolved_area_filters = list(
                    dict.fromkeys((*exact_source_areas, *api_plan.matched_areas))
                )
                candidates = build_candidates_from_groups(
                    indexed_groups,
                    targets,
                    record_type=args.record_type,
                    area_filters=resolved_area_filters or args.area,
                    scope_filters=args.scope,
                    reviewed_scope_only=args.reviewed_scope_only,
                    include_inactive=args.include_inactive,
                )
                candidate_catalog = list(candidates)
            api_info.update(
                {
                    "status": "planned",
                    "query_plan": api_plan.to_dict(),
                    "retrieval_query": retrieval_query,
                    "area_resolution": {
                        "mode": "llm_controlled_vocabulary",
                        "requested": list(args.area),
                        "matched": list(api_plan.matched_areas),
                        "effective": (
                            resolved_area_filters or list(args.area)
                            if args.area
                            else []
                        ),
                        "available_label_count": len(available_area_labels),
                    },
                }
            )
            _emit_retrieval_event(
                event_callback,
                "progress",
                stage="llm",
                status="done",
                matched_area_count=len(api_plan.matched_areas),
            )
        except ApiAssistantError as exc:
            if search_index is not None:
                search_index.close()
            parser.error(f"LLM 查询理解失败：{exc}")

    # Once the LLM plan exists, web evidence, the exact query embedding, and
    # LightRAG are independent remote-bound operations. Start them together so
    # network waits overlap; ranking and fusion below remain deterministic.
    pipeline_executor: ThreadPoolExecutor | None = None
    search_future: Future | None = None
    if api_plan is not None and api_assistant is not None:
        from .api_assistant import collect_search_evidence

        pipeline_executor = ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="venue-pipeline"
        )
        _emit_retrieval_event(
            event_callback,
            "progress",
            stage="search",
            status="running",
            phase="evidence",
        )
        search_future = pipeline_executor.submit(
            collect_search_evidence,
            api_plan,
            query,
            api_config,
            api_cache_dir,
            query_limit=args.api_search_query_limit,
            results_per_query=args.api_search_results,
            timeout=args.api_timeout,
        )

    def shutdown_pipeline() -> None:
        nonlocal pipeline_executor
        if pipeline_executor is not None:
            pipeline_executor.shutdown(wait=True, cancel_futures=True)
            pipeline_executor = None

    query_vector: list[float] | None = None
    vector_provider_fingerprint: str | None = None
    vector_info: dict[str, object] = {"enabled": False}
    vector_future: Future | None = None
    query_embedding_cache_path: Path | None = None
    lightrag_embedding_cache_path: Path | None = None
    if args.vector_search:
        from .embeddings import (
            EmbeddingError,
            OpenAICompatibleEmbeddingProvider,
            default_embedding_cache_path,
            default_graph_embedding_cache_path,
            default_query_embedding_cache_path,
            embed_query,
            embed_query_graph,
            load_embedding_config,
        )

        _emit_retrieval_event(
            event_callback,
            "progress",
            stage="vector",
            status="running",
        )
        try:
            embedding_config = load_embedding_config(forced_config_path)
            embedding_provider = OpenAICompatibleEmbeddingProvider(embedding_config)
            vector_metadata = search_index.vector_metadata()
            if not vector_metadata:
                raise EmbeddingError(
                    "向量尚未构建；请先运行 "
                    "python3 -m scripts.prepare_retrieval --api-config llmapi.json"
                )
            if (
                vector_metadata["vector_provider_fingerprint"]
                != embedding_provider.fingerprint
            ):
                raise EmbeddingError(
                    "embedding 配置与现有向量索引不匹配；请使用同一配置重建向量"
                )
            vector_provider_fingerprint = embedding_provider.fingerprint
            query_embedding_cache_path = args.query_embedding_cache or (
                default_query_embedding_cache_path(args.data_dir)
                if using_graph_backend
                else default_embedding_cache_path(args.data_dir)
            )
            lightrag_embedding_cache_path = args.lightrag_embedding_cache or (
                default_graph_embedding_cache_path(args.data_dir)
                if using_graph_backend
                else default_embedding_cache_path(args.data_dir)
            )
            if candidates:
                embedding_function = (
                    embed_query_graph if using_graph_backend else embed_query
                )
                if pipeline_executor is None:
                    pipeline_executor = ThreadPoolExecutor(
                        max_workers=2, thread_name_prefix="venue-pipeline"
                    )
                vector_future = pipeline_executor.submit(
                    embedding_function,
                    semantic_query,
                    embedding_provider,
                    query_embedding_cache_path,
                )
            vector_info = {
                "enabled": True,
                "model": vector_metadata["vector_model"],
                "dimensions": int(vector_metadata["vector_dimensions"]),
                "candidate_limit": args.vector_limit,
                "minimum_similarity": args.vector_min_similarity,
                "weight": args.vector_weight,
                "scan_mode": (
                    "approximate" if args.approximate_vector_search else "exact"
                ),
            }
        except (
            EmbeddingError,
            OSError,
            sqlite3.Error,
            ValueError,
            RuntimeError,
        ) as exc:
            shutdown_pipeline()
            if search_index is not None:
                search_index.close()
            parser.error(f"向量语义召回不可用：{exc}")
    lightrag_recall = None
    lightrag_info: dict[str, object] = {"enabled": False}
    lightrag_future: Future | None = None
    if query:
        from .lightrag import (
            LightRAGRuntimeError,
            default_lightrag_working_dir,
            query_lightrag,
        )

        working_dir = (
            args.lightrag_working_dir
            or default_lightrag_working_dir(args.data_dir)
        )
        candidate_entity_ids = [
            min(record.row_id for record in candidate.records)
            for candidate in candidate_catalog
        ]
        high_level_keywords = [
            api_plan.intent_summary_zh,
            *(
                QUERY_CONCEPT_LABELS[tag]
                for tag in api_plan.topic_tags
                if tag in QUERY_CONCEPT_LABELS
            ),
        ]
        low_level_keywords = [
            *api_plan.keywords_zh,
            *api_plan.keywords_en,
            *api_plan.technical_phrases,
        ]
        _emit_retrieval_event(
            event_callback,
            "progress",
            stage="graph",
            status="running",
        )
        try:
            if pipeline_executor is None:
                pipeline_executor = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="venue-pipeline"
                )
            lightrag_future = pipeline_executor.submit(
                query_lightrag,
                semantic_query,
                working_dir,
                persistent_path,
                forced_config_path,
                lightrag_embedding_cache_path,
                high_level_keywords=high_level_keywords,
                low_level_keywords=low_level_keywords,
                allowed_entity_ids=candidate_entity_ids,
                top_k=args.lightrag_top_k,
                chunk_top_k=args.lightrag_chunk_top_k,
            )
        except (LightRAGRuntimeError, OSError, ValueError, RuntimeError) as exc:
            shutdown_pipeline()
            if search_index is not None:
                search_index.close()
            parser.error(f"LightRAG 强制检索不可用：{exc}")

    if vector_future is not None:
        try:
            query_vector = vector_future.result()
            _emit_retrieval_event(
                event_callback,
                "progress",
                stage="vector",
                status="done",
                model=vector_info["model"],
                dimensions=vector_info["dimensions"],
            )
        except (
            EmbeddingError,
            OSError,
            sqlite3.Error,
            ValueError,
            RuntimeError,
        ) as exc:
            shutdown_pipeline()
            if search_index is not None:
                search_index.close()
            parser.error(f"向量语义召回不可用：{exc}")
    elif args.vector_search:
        _emit_retrieval_event(
            event_callback,
            "progress",
            stage="vector",
            status="done",
            model=vector_info.get("model", ""),
            dimensions=vector_info.get("dimensions", 0),
        )

    if lightrag_future is not None:
        try:
            lightrag_recall = lightrag_future.result()
            lightrag_info = {
                **lightrag_recall.to_info(),
                "working_dir": str(working_dir.resolve()),
                "top_k": args.lightrag_top_k,
                "chunk_top_k": args.lightrag_chunk_top_k,
                "weight": args.lightrag_weight,
            }
        except (LightRAGRuntimeError, OSError, ValueError, RuntimeError) as exc:
            shutdown_pipeline()
            if search_index is not None:
                search_index.close()
            parser.error(f"LightRAG 强制检索不可用：{exc}")
    if query:
        if search_index is not None:
            lexical_limit = (
                None
                if args.show_all or args.candidate_pool == 0
                else max(args.candidate_pool, args.limit)
            )
            try:
                candidates = rank_candidates_indexed(
                    candidates,
                    retrieval_query,
                    search_index,
                    include_official_scope=effective_match_official_scope,
                    lexical_limit=lexical_limit,
                    query_vector=query_vector,
                    vector_provider_fingerprint=vector_provider_fingerprint,
                    vector_limit=args.vector_limit,
                    vector_min_similarity=args.vector_min_similarity,
                    vector_weight=args.vector_weight,
                    approximate_vector_recall=args.approximate_vector_search,
                    lightrag_entity_ids=lightrag_recall.entity_ids,
                    lightrag_scores=lightrag_recall.scores,
                    lightrag_channels=lightrag_recall.channels,
                    lightrag_weight=args.lightrag_weight,
                    constraint_query=query,
                    additional_query_concepts=additional_query_concepts,
                )
            except (sqlite3.Error, ValueError, RuntimeError) as exc:
                shutdown_pipeline()
                if search_index is not None:
                    search_index.close()
                parser.error(f"强制图谱/向量/LightRAG 召回失败：{exc}")
        else:
            candidates = rank_candidates(
                candidates,
                retrieval_query,
                include_official_scope=effective_match_official_scope,
                constraint_query=query,
                additional_query_concepts=additional_query_concepts,
            )
        _emit_retrieval_event(
            event_callback,
            "progress",
            stage="graph",
            status="done",
            recalled_venue_count=lightrag_info.get("recalled_venue_count", 0),
        )
        preliminary = candidates if args.show_all else candidates[: args.limit]
        _emit_retrieval_event(
            event_callback,
            "results",
            phase="preliminary",
            payload={
                "targets": [target.label for target in targets],
                "query": query,
                "area_filters": list(args.area),
                "scope_filters": list(args.scope),
                "search_backend": "lightrag_mix+property_graph_exact_vector+llm+search_api",
                "streaming_phase": "preliminary",
                "vector_search": vector_info,
                "lightrag": lightrag_info,
                "api_assisted_search": api_info,
                "record_type_filter": args.record_type,
                "total": len(candidates),
                "displayed": len(preliminary),
                "notice": "初步结果尚未经 Search API 证据与 LLM 最终重排。",
                "results": [candidate_to_dict(candidate) for candidate in preliminary],
            },
        )
        if api_plan is not None and api_assistant is not None:
            from .api_assistant import (
                ApiAssistantError,
                CandidateContext,
                fuse_entity_rankings,
                hinted_entity_ids,
            )

            def api_context(candidate: VenueCandidate) -> CandidateContext:
                source_urls = _unique(
                    (
                        *candidate.curated_secondary_source_urls,
                        *(
                            record.official_scope_url
                            for record in candidate.records
                            if record.official_scope_url
                        ),
                    )
                )
                return CandidateContext(
                    entity_id=min(record.row_id for record in candidate.records),
                    name=candidate.name,
                    abbreviation=candidate.abbreviation,
                    record_type=candidate.record_type,
                    classification_scope="；".join(
                        candidate.taxonomy_scopes or candidate.areas
                    ),
                    reviewed_scope="；".join(candidate.curated_scopes),
                    reviewed_topics="；".join(candidate.curated_topics),
                    automatic_scope="；".join(
                        candidate.official_scope_candidates[:2]
                    ),
                    source_urls=tuple(source_urls),
                )

            try:
                if search_future is None:
                    raise ApiAssistantError("Search API 并行任务未启动")
                evidence, attempted_queries = search_future.result()
                shutdown_pipeline()
                _emit_retrieval_event(
                    event_callback,
                    "progress",
                    stage="search",
                    status="running",
                    phase="rerank",
                    search_result_count=len(evidence),
                )
                all_contexts = [api_context(candidate) for candidate in candidate_catalog]
                context_by_id = {context.entity_id: context for context in all_contexts}
                ranked_ids = [
                    min(record.row_id for record in candidate.records)
                    for candidate in candidates
                ]
                hinted_ids = hinted_entity_ids(
                    all_contexts, api_plan.venue_hints, evidence
                )
                rerank_ids, recall_pool_info = build_multichannel_recall_pool(
                    candidates,
                    candidate_catalog,
                    query=query,
                    matched_areas=api_plan.matched_areas,
                    hinted_entity_ids=hinted_ids,
                    limit=args.api_candidate_limit,
                    adaptive=not args.fixed_recall_budget,
                    query_ambiguity=api_plan.ambiguity,
                    query_cross_disciplinary=api_plan.cross_disciplinary,
                )
                rerank_contexts = [
                    context_by_id[entity_id]
                    for entity_id in rerank_ids
                    if entity_id in context_by_id
                ]
                recall_candidate_by_id = {
                    min(record.row_id for record in candidate.records): candidate
                    for candidate in candidate_catalog
                }
                _emit_retrieval_event(
                    event_callback,
                    "results",
                    phase="recall_pool",
                    payload={
                        "targets": [target.label for target in targets],
                        "query": query,
                        "search_backend": "multichannel_recall_pool",
                        "streaming_phase": "recall_pool",
                        "total": len(rerank_ids),
                        "displayed": len(rerank_ids),
                        "multichannel_recall": recall_pool_info,
                        "results": [
                            candidate_to_dict(recall_candidate_by_id[entity_id])
                            for entity_id in rerank_ids
                            if entity_id in recall_candidate_by_id
                        ],
                    },
                )
                api_scores = api_assistant.rerank_candidates(
                    query, api_plan, rerank_contexts, evidence
                )
                fused_ids = fuse_entity_rankings(
                    ranked_ids,
                    api_scores,
                    api_weight=args.api_rerank_weight,
                )
                explain_method = (
                    None
                    if args.no_api_explanations
                    else getattr(api_assistant, "explain_candidates", None)
                )
                explained_candidate_count = 0
                if callable(explain_method):
                    explain_ids = [
                        entity_id
                        for entity_id in fused_ids
                        if entity_id in api_scores and entity_id in context_by_id
                    ][:10]
                    _emit_retrieval_event(
                        event_callback,
                        "progress",
                        stage="search",
                        status="running",
                        phase="explain",
                        candidate_count=len(explain_ids),
                    )
                    explained_scores = explain_method(
                        query,
                        api_plan,
                        [context_by_id[entity_id] for entity_id in explain_ids],
                        evidence,
                        api_scores,
                    )
                    api_scores.update(explained_scores)
                    explained_candidate_count = len(explained_scores)
                candidate_by_id = {
                    min(record.row_id for record in candidate.records): candidate
                    for candidate in candidate_catalog
                }
                for entity_id, api_score in api_scores.items():
                    candidate = candidate_by_id.get(entity_id)
                    if candidate is None:
                        continue
                    candidate.api_relevance = api_score.relevance
                    candidate.api_confidence = api_score.confidence
                    candidate.api_reason = api_score.reason
                    candidate.api_evidence_urls = list(api_score.evidence_urls)
                    if "llm_api_rerank" not in candidate.matched_fields:
                        candidate.matched_fields.append("llm_api_rerank")
                    if api_score.evidence_urls and "search_api_evidence" not in candidate.matched_fields:
                        candidate.matched_fields.append("search_api_evidence")
                candidates = [
                    candidate_by_id[entity_id]
                    for entity_id in fused_ids
                    if entity_id in candidate_by_id
                ]
                api_info.update(
                    {
                        "status": "ok",
                        "search_queries": attempted_queries,
                        "search_result_count": len(evidence),
                        # Keep the complete evidence set in machine-readable
                        # output. Benchmark leakage audits must inspect every
                        # item the reranker saw, not an arbitrary display cap.
                        "search_results": [item.to_dict() for item in evidence],
                        "reranked_candidate_count": len(api_scores),
                        "rerank_concurrency": 2,
                        "rerank_mode": "compact_score_then_top10_explain",
                        "explanations_skipped": bool(args.no_api_explanations),
                        "explained_candidate_count": explained_candidate_count,
                        "hinted_candidate_count": len(hinted_ids),
                        "multichannel_recall": recall_pool_info,
                    }
                )
                _emit_retrieval_event(
                    event_callback,
                    "progress",
                    stage="search",
                    status="done",
                    search_result_count=len(evidence),
                    reranked_candidate_count=len(api_scores),
                )
            except ApiAssistantError as exc:
                shutdown_pipeline()
                if search_index is not None:
                    search_index.close()
                parser.error(f"Search API/LLM 证据重排失败：{exc}")
    else:
        candidates = sort_unranked_candidates(candidates, targets)
    shutdown_pipeline()
    if search_index is not None:
        search_index.close()

    total = len(candidates)
    displayed = candidates if args.show_all else candidates[: args.limit]
    empty_hint = ""
    if not candidates:
        if args.scope:
            empty_hint = "提示：目标等级中已审核的细粒度范围没有匹配；这也可能表示该部分数据尚未覆盖。"
        elif args.reviewed_scope_only:
            empty_hint = "提示：当前等级、类型或分类中尚无已审核细粒度范围。"
        elif args.record_type == "journal" and all(
            target.dataset == "ccf" for target in targets
        ):
            empty_hint = "提示：当前 CCF 数据文件只包含会议；可改用 --record-type all，或查询 TH-CPL/CAS/JCR 期刊。"
        elif query and any(_article_intents(query)):
            empty_hint = "提示：未找到同时符合当前主题和明确稿件类型的已审核候选。"
        elif query:
            empty_hint = "提示：等级候选中没有方向词重合；可增加研究对象、方法和应用场景关键词。"
        else:
            empty_hint = "提示：没有记录同时满足当前等级、类型和分类过滤条件。"
    if args.format == "json":
        payload = {
            "targets": [target.label for target in targets],
            "query": query,
            "area_filters": args.area,
            "scope_filters": args.scope,
            "reviewed_scope_only": args.reviewed_scope_only,
            "match_official_scope": effective_match_official_scope,
            "reviewed_scope_catalog_available": scope_catalog_available,
            "search_backend": (
                "lightrag_mix+property_graph_exact_vector+llm+search_api"
                if query
                else backend_name
                if index_used
                else "csv_memory"
            ),
            "candidate_pool": args.candidate_pool if query else None,
            "vector_search": vector_info,
            "lightrag": lightrag_info,
            "api_assisted_search": api_info,
            "record_type_filter": args.record_type,
            "total": total,
            "displayed": len(displayed),
            "notice": "本结果仅用于辅助选刊，投稿前必须人工核验最新官网信息。",
            "results": [candidate_to_dict(candidate) for candidate in displayed],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        write_csv_output(displayed)
    else:
        sys.stdout.write(
            render_text(
                displayed,
                total=total,
                targets=targets,
                query=query,
                area_filters=args.area,
                scope_filters=args.scope,
                names_only=args.names_only,
                max_scope_chars=args.max_scope_chars,
                empty_hint=empty_hint,
                catalog_warning=(
                    " ".join(
                        warning
                        for warning in (
                            (
                                "警告：当前数据目录缺少已审核细粒度范围，"
                                "本次仅使用榜单基础分类。"
                                if not scope_catalog_available
                                else ""
                            ),
                            index_warning,
                            api_warning,
                        )
                        if warning
                    )
                ),
            )
        )
    return 0


def _ranking_labels(records: Iterable[VenueRecord]) -> list[str]:
    ordered = sorted(
        {(record.dataset, record.level, record.version_year) for record in records},
        key=lambda item: (DATASET_ORDER.get(item[0], 99), item[2], item[1]),
    )
    return [ranking_label(dataset, level, year) for dataset, level, year in ordered]


def _unique(values: Iterable[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        value = normalize_space(value)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _name_display_quality(value: str) -> tuple[int, int, str]:
    letters = [char for char in value if char.isalpha()]
    all_upper = bool(letters) and all(not char.islower() for char in letters)
    return int(all_upper), len(value), value.casefold()


def _truncate(value: str, max_chars: int) -> str:
    return value if len(value) <= max_chars else value[: max_chars - 1].rstrip() + "…"


if __name__ == "__main__":
    raise SystemExit(main())
