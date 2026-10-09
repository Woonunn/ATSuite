import tempfile
import unittest
from pathlib import Path
from atsuite.execution.build import build_key
from atsuite.deploy import _artifact_image_ref

class ArtifactIsolation(unittest.TestCase):
    def test_source_and_dependency_changes_invalidate_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'; path.write_text('{}')
            candidate={'provider':'aws_lambda','family':'faas','logical_topology':[['a']],'runtime_architecture':'linux/amd64',
                'bundle_digest':'one'}
            a=build_key(candidate); candidate['bundle_digest']='two'; self.assertNotEqual(a,build_key(candidate))
    def test_invalid_digest_is_rejected(self):
        with self.assertRaises(RuntimeError): _artifact_image_ref({'repository':'r','digest':'sha256:abc'})
