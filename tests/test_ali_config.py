import contextlib
import io
import os
import unittest
from unittest import mock

from atsuite.ali.fc import AliFC
from atsuite.analysis.collectors import create_collector


class AliLogConfigurationTests(unittest.TestCase):
    def test_deployment_and_analysis_use_the_same_log_project(self):
        for configured in (False, True):
            for typ in ("function", "mcp"):
                with self.subTest(configured=configured, typ=typ):
                    env = {"OSS_ACCESS_KEY_ID": "test", "OSS_ACCESS_KEY_SECRET": "test"}
                    if configured:
                        env.update(ATSUITE_ALI_SLS_PROJECT="wabber", ATSUITE_ALI_SLS_LOCATION="cn-hangzhou")
                    project = "wabber" if configured else "atsuite"
                    location = "cn-hangzhou" if configured else "us-east-1"
                    client = mock.Mock()
                    deployment = AliFC("demo", [], "demo", client, typ=typ)
                    with mock.patch.dict(os.environ, env, clear=True), \
                         mock.patch("atsuite.ali.fc.AliSLS") as sls, \
                         mock.patch.object(deployment, "push_image", return_value="registry/demo:latest"), \
                         mock.patch.object(deployment, "create_trigger", return_value="https://example.test"), \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(deployment.deploy(), "https://example.test")
                        collector = create_collector("ali_sls")
                    sls.assert_called_once_with(project=project, location=location)
                    sls.return_value.create_project.assert_called_once_with()
                    sls.return_value.create_logstore.assert_called_once_with(f"demo-{typ}")
                    request = client.create_function_with_options.call_args.args[0]
                    self.assertEqual(request.body.log_config.project, project)
                    self.assertEqual(request.body.log_config.logstore, f"demo-{typ}")
                    self.assertEqual((collector.project, collector.location), (project, location))
