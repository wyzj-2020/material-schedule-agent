"""pytest 公共配置：把项目根目录加入 sys.path，并提供工具上下文 fixture。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.pipeline import DATA_DIR, build_ctx  # noqa: E402


@pytest.fixture
def ctx(tmp_path):
    """一个使用临时草稿文件的工具上下文（不污染 data/drafts.json）。"""
    return build_ctx(data_dir=DATA_DIR, drafts_file=str(tmp_path / "drafts.json"))
