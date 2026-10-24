"""Include lightweight worker contracts in ordinary toolkit discovery."""
import importlib.util
from pathlib import Path
import unittest


def load_tests(loader, tests, pattern):
    path = Path(__file__).resolve().parents[1] / "tools/extraction/test_docling_worker.py"
    spec = importlib.util.spec_from_file_location("docling_worker_contract_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return loader.loadTestsFromModule(module)
