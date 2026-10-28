"""Ensure submission packaging omits local AI credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import package_submission


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patch=patch.object(package_submission,'ROOT',self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def write(self,name,content='{}'):
        path=self.root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(content,encoding='utf-8')
        return path

    def test_local_and_environment_files_omitted(self):
        for name in ('ai.local.json','custom.local.json','.env','.env.school','data/private.json'):
            self.assertFalse(package_submission.include_file(self.write(name)),name)
        self.assertTrue(package_submission.include_file(self.write('.env.example')))

    def test_custom_config_name_with_key_omitted(self):
        path=self.write('school-connection.json',json.dumps({'mode':'remote','api_key':'test-credential'}))
        self.assertFalse(package_submission.include_file(path))

    def test_empty_key_template_and_test_evidence_included(self):
        for name,value in (('ai.example.json',{'api_key':''}),('docs/test_results.json',{'passed':True})):
            self.assertTrue(package_submission.include_file(self.write(name,json.dumps(value))))

    def test_database_files_outside_default_directory_are_omitted(self):
        for name in ('scratch/pages.dat','scratch/redo.wal','custom.db','custom.sqlite',
                     'nested/custom.sqlite3','custom.db-wal','custom.db-shm',
                     'nested/custom.sqlite3-wal','nested/custom.sqlite3-shm'):
            self.assertFalse(package_submission.include_file(self.write(name)),name)

    def test_windows_case_variants_of_private_files_are_omitted(self):
        for name in ('AI.LOCAL.JSON','.ENV.SCHOOL','SCRATCH/PAGES.DAT','SCRATCH/REDO.WAL',
                     'CUSTOM.DB','DATA/PRIVATE.JSON'):
            self.assertFalse(package_submission.include_file(self.write(name)),name)


if __name__=='__main__':
    unittest.main()
