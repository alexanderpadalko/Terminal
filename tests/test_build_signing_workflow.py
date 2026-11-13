"""Prevent accidental release of an unsigned EXE as SIGNED.

These checks only validate GitHub Actions release guardrails. They do not
replace Authenticode verification with the real publisher certificate.
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"


class TestWindowsSigningRelease(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_signing_requires_main_branch_and_explicit_opt_in(self):
        code = self.workflow
        gate = ("github.event_name != 'pull_request' && "
                "github.ref == 'refs/heads/main' && "
                "vars.FLOWDESK_SIGNING_ENABLED == 'true'")
        self.assertEqual(code.count("if: "+gate),4)
        self.assertIn("id-token: write",code)
        self.assertIn("azure/login@v3",code)
        self.assertIn("azure/artifact-signing-action@v2",code)
        self.assertIn("exclude-azure-cli-credential: false",code)

    def test_sign_then_verify_before_publishing(self):
        code=self.workflow
        sign=code.index("- name: Sign FlowDesk.exe with Microsoft Artifact Signing")
        verify=code.index("- name: Verify Authenticode trust and RFC3161 timestamp")
        manifest=code.index("- name: Record signature state and SHA256")
        upload=code.index("- name: Publish standalone Windows folder")
        self.assertTrue(sign<verify<manifest<upload)
        self.assertIn("Get-AuthenticodeSignature",code)
        self.assertIn("TimeStamperCertificate",code)
        self.assertIn("throw 'Signed release verification failed.'",code)
        self.assertIn("timestamp-rfc3161: http://timestamp.acs.microsoft.com",code)

    def test_never_label_unsigned_release_signed(self):
        text=self.workflow
        self.assertIn("Windows-x64-SIGNED",text)
        self.assertIn("Windows-x64-UNSIGNED",text)
        self.assertIn("$(if ($enabled) { 'SIGNED' } else { 'UNSIGNED' })",text)
        self.assertIn("build-signature.json",text)
        self.assertIn("Get-FileHash",text)
        self.assertNotIn("name: FlowDesk-0.2.26-Finam-Windows-x64\n",text)


if __name__=="__main__":
    unittest.main()
