"""Local configuration files, independent of API and cloud access."""
import contextlib
import importlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class LocalEnvContracts(unittest.TestCase):
    def config(self):
        return importlib.import_module('atsuite.config')

    def test_project_file_supports_quoted_values_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
            path=Path(tmp)/'.env'
            path.write_text('DEEPSEEK_API_KEY="contract-key"\nATSUITE_ADVISOR_MODEL=deepseek-flash\n')
            config=self.config();output=io.StringIO()
            with patch.object(config,'PROJECT_ENV_FILE',path),contextlib.redirect_stdout(output):
                self.assertTrue(config.load_local_env())
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'contract-key')
            self.assertEqual(os.environ['ATSUITE_ADVISOR_MODEL'],'deepseek-flash')
            self.assertEqual(output.getvalue(),'')

    def test_existing_environment_including_empty_values_wins(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'DEEPSEEK_API_KEY':'existing','ATSUITE_ADVISOR_MODEL':''},clear=True):
            path=Path(tmp)/'.env';path.write_text('DEEPSEEK_API_KEY=file-key\nATSUITE_ADVISOR_MODEL=deepseek-flash\n')
            config=self.config()
            with patch.object(config,'PROJECT_ENV_FILE',path):config.load_local_env()
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'existing')
            self.assertEqual(os.environ['ATSUITE_ADVISOR_MODEL'],'')

    def test_missing_default_file_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
            config=self.config()
            with patch.object(config,'PROJECT_ENV_FILE',Path(tmp)/'missing'):
                self.assertFalse(config.load_local_env())
            self.assertNotIn('DEEPSEEK_API_KEY',os.environ)

    def test_working_directory_env_does_not_override_project_file(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
            root=Path(tmp);trusted=root/'project.env';trusted.write_text('DEEPSEEK_API_KEY=trusted\n')
            bundle=root/'uploaded';bundle.mkdir();(bundle/'.env').write_text('DEEPSEEK_API_KEY=untrusted\n')
            config=self.config()
            with patch.object(config,'PROJECT_ENV_FILE',trusted),contextlib.chdir(bundle):config.load_local_env()
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'trusted')

    def test_explicit_file_and_disabled_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'custom.env';path.write_text('DEEPSEEK_API_KEY=custom\n')
            for disabled in (False,True):
                with self.subTest(disabled=disabled),patch.dict(os.environ,{'ATSUITE_ENV_FILE':str(path),'PYTHON_DOTENV_DISABLED':'1' if disabled else '0'},clear=True):
                    self.config().load_local_env()
                    self.assertEqual(os.environ.get('DEEPSEEK_API_KEY'),None if disabled else 'custom')

    def test_entrypoints_load_same_file_even_for_help(self):
        modules=('atsuite.cli.recommend','atsuite.cli.serve','atsuite.cli.worker')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.env';path.write_text('DEEPSEEK_API_KEY=contract-key\n')
            for name in modules:
                module=importlib.import_module(name)
                with self.subTest(entrypoint=name),patch.dict(os.environ,{'ATSUITE_ENV_FILE':str(path)},clear=True),contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_result:module.main(['--help'])
                    self.assertEqual(exit_result.exception.code,0)
                    self.assertEqual(os.environ.get('DEEPSEEK_API_KEY'),'contract-key')

    def test_check_loads_file_without_live_call(self):
        from tools.advisor_check import main
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.env';path.write_text('DEEPSEEK_API_KEY=contract-key\nATSUITE_ADVISOR_MODEL=deepseek-flash\n')
            output=io.StringIO()
            with patch.dict(os.environ,{'ATSUITE_ENV_FILE':str(path)},clear=True),patch('openai.OpenAI') as constructor,contextlib.redirect_stdout(output):
                self.assertEqual(main([]),0)
            constructor.assert_not_called()
            self.assertIn('configuration_ready',output.getvalue())
            self.assertNotIn('contract-key',output.getvalue())

    def test_direct_web_factory_loads_file_without_model_request(self):
        from atsuite.server.app import create_app
        from tests.fake_execution_backend import FakeExecutionBackend
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'config.env';path.write_text('DEEPSEEK_API_KEY=contract-key\n')
            with patch.dict(os.environ,{'ATSUITE_ENV_FILE':str(path)},clear=True),patch('openai.OpenAI') as constructor:
                app=create_app(artifact_root=root/'artifacts',execution_backend=FakeExecutionBackend())
                self.assertEqual(os.environ.get('DEEPSEEK_API_KEY'),'contract-key')
                app.state.store.close()
            constructor.assert_not_called()
