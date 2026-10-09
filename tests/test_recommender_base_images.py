import json
import unittest
from types import SimpleNamespace
from atsuite.execution.base_images import freeze_base_image
from atsuite.execution.build import build_key

class BaseImages(unittest.TestCase):
    def runner(self,arch='amd64'):
        def run(command,**kwargs):
            if '--format' in command:return SimpleNamespace(stdout=json.dumps({'digest':'sha256:'+'a'*64}))
            return SimpleNamespace(stdout=json.dumps({'manifests':[{'digest':'sha256:'+'b'*64,'platform':{'os':'linux','architecture':arch}}]}))
        return run
    def test_tag_is_frozen_to_concrete_platform_digest(self):
        self.assertEqual(freeze_base_image('registry.test/python:3.12','linux/amd64',runner=self.runner()),'registry.test/python@sha256:'+'b'*64)
    def test_wrong_platform_and_short_hash_are_rejected(self):
        with self.assertRaises(RuntimeError):freeze_base_image('registry.test/python:3.12','linux/arm64',runner=self.runner())
        with self.assertRaises(ValueError):freeze_base_image('registry.test/python@sha256:abc','linux/amd64')
    def test_base_digest_changes_build_key_without_resource_fields(self):
        candidate={'provider':'aws_lambda','family':'faas','logical_topology':[['a']],'runtime_architecture':'linux/amd64','bundle_digest':'bundle','base_image_digest':'one'}
        original=build_key(candidate);candidate['base_image_digest']='two'
        self.assertNotEqual(original,build_key(candidate))
