import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import send_alert

CONFIG = Path(__file__).resolve().parent.parent / "config.json"
ENV = {"SMTP_USERNAME": "sender@example.test", "SMTP_PASSWORD": "app-password", "ALERT_EMAIL_TO": " a@example.test, b@example.test ,"}


class SendAlertTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(send_alert.smtplib, "SMTP_SSL")
        self.smtp_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.smtp = self.smtp_class.return_value.__enter__.return_value

    def run_main(self, argv, env):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = send_alert.main(argv + ["--config", str(CONFIG)], env)
        return code, out.getvalue(), err.getvalue()

    def test_sends_report_to_bcc_recipients(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "changes.json"
            report.write_text(json.dumps({"subject": "[automated] Vintage Vikings schedule update", "body": "Changed: ...\n"}))
            code, out, _ = self.run_main([str(report)], ENV)
        self.assertEqual(code, 0)
        self.assertIn("to 2 recipient(s)", out)
        self.smtp_class.assert_called_once()
        self.assertEqual(self.smtp_class.call_args.args, ("smtp.gmail.com", 465))
        self.smtp.login.assert_called_once_with("sender@example.test", "app-password")
        message = self.smtp.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "[automated] Vintage Vikings schedule update")
        self.assertEqual(message["To"], "sender@example.test")
        self.assertEqual(message["Bcc"], "a@example.test, b@example.test")
        self.assertEqual(message.get_content(), "Changed: ...\n")

    def test_test_alert_and_custom_server(self):
        code, _, _ = self.run_main(["--test"], {**ENV, "SMTP_HOST": "smtp.example.test", "SMTP_PORT": "2465"})
        self.assertEqual(code, 0)
        self.assertEqual(self.smtp_class.call_args.args, ("smtp.example.test", 2465))
        message = self.smtp.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "[automated] Vintage Vikings schedule update (test)")
        self.assertIn("This is a test", message.get_content())
        self.assertIn(send_alert.EXAMPLE_CHANGE, message.get_content())

    def test_missing_settings_fail_without_sending(self):
        code, _, err = self.run_main(["--test"], {"SMTP_USERNAME": "x", "SMTP_PASSWORD": "", "ALERT_EMAIL_TO": ""})
        self.assertEqual(code, 2)
        self.assertIn("SMTP_PASSWORD, ALERT_EMAIL_TO", err)
        code, _, err = self.run_main(["--test"], {**ENV, "ALERT_EMAIL_TO": " , "})
        self.assertEqual(code, 2)
        self.smtp_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
