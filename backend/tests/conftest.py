"""Shared test fixtures. Mocks riva modules so tests can run without GPU/Riva."""

import sys
from unittest.mock import MagicMock

# Mock the entire riva module tree before any backend code is imported
_mock_riva = MagicMock()
sys.modules["riva"] = _mock_riva
sys.modules["riva.client"] = _mock_riva.client
sys.modules["riva.client.proto"] = _mock_riva.client.proto
sys.modules["riva.client.proto.riva_asr_pb2"] = _mock_riva.client.proto.riva_asr_pb2
sys.modules["riva.client.proto.riva_nmt_pb2"] = _mock_riva.client.proto.riva_nmt_pb2
