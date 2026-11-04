import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import httpx
from openai import OpenAI

from agents import ai_config, openai_physical_verification as physical


class HTTPError(Exception):
    def __init__(self, status=429, headers=None):
        super().__init__("API error")
        self.status_code = status
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


class RateLimitRetryTests(unittest.TestCase):
    def setUp(self):
        sleep = patch.object(ai_config.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def test_numeric_retry_after_resends_exact_request(self):
        request = Mock(side_effect=[HTTPError(headers={"Retry-After": "42.5"}), "success"])
        payload = {"model": "test", "messages": [{"role": "user", "content": "same request"}]}
        self.assertEqual(ai_config.request_with_retry(request, **payload), "success")
        self.sleep.assert_called_once_with(42.5)
        self.assertEqual(request.call_args_list, [call(**payload), call(**payload)])

    def test_http_date_retry_after(self):
        now = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)
        error = HTTPError(headers={"retry-after": "Mon, 02 Nov 2026 22:01:30 GMT"})
        request = Mock(side_effect=[error, "success"])
        with patch.object(ai_config, "datetime") as clock:
            clock.now.return_value = now
            ai_config.request_with_retry(request)
        self.sleep.assert_called_once_with(90)

    def test_zero_or_past_retry_after_does_not_use_backoff(self):
        for value in ("0", "Mon, 02 Nov 2020 22:01:30 GMT"):
            with self.subTest(value=value):
                self.sleep.reset_mock()
                request = Mock(side_effect=[HTTPError(headers={"Retry-After": value}), "success"])
                ai_config.request_with_retry(request)
                self.sleep.assert_called_once_with(0)

    def test_missing_header_uses_increasing_delays_and_stops_after_four_retries(self):
        error = HTTPError()
        request = Mock(side_effect=error)
        with patch.object(ai_config.random, "uniform", return_value=0.5), self.assertRaises(HTTPError) as raised:
            ai_config.request_with_retry(request)
        self.assertIs(raised.exception, error)
        self.assertEqual(request.call_count, 5)
        self.assertEqual(self.sleep.call_args_list, [call(15.5), call(30.5), call(60.5), call(120.5)])

    def test_invalid_header_uses_backoff(self):
        for value in ("", "-1", "nan", "inf", "not a date"):
            with self.subTest(value=value), patch.object(ai_config.random, "uniform", return_value=0):
                self.sleep.reset_mock()
                request = Mock(side_effect=[HTTPError(headers={"Retry-After": value}), "success"])
                ai_config.request_with_retry(request)
                self.sleep.assert_called_once_with(15)

    def test_each_response_can_supply_a_different_delay(self):
        request = Mock(side_effect=[HTTPError(headers={"Retry-After": "10"}),
                                    HTTPError(headers={"Retry-After": "20"}), "success"])
        ai_config.request_with_retry(request)
        self.assertEqual(self.sleep.call_args_list, [call(10), call(20)])

    def test_other_errors_are_not_retried(self):
        for error in (HTTPError(401), HTTPError(500), ValueError("bad response")):
            with self.subTest(error=error), self.assertRaises(type(error)):
                request = Mock(side_effect=error)
                ai_config.request_with_retry(request)
            request.assert_called_once()
        self.sleep.assert_not_called()

    def test_responses_api_is_also_retried(self):
        client = Mock()
        client.responses.create.side_effect = [HTTPError(headers={"Retry-After": "1"}), "success"]
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(ai_config.create_response(client, model="test", input=[]), "success")
        self.sleep.assert_called_once_with(1)
        self.assertEqual(client.responses.create.call_count, 2)

    def test_physical_agent_recovers_from_real_sdk_429_without_restarting_design(self):
        requests = []
        plan = {"summary": "No supported repair", "actions": []}

        def handle(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, headers={"Retry-After": "12"}, json={"detail": "Rate limit reached"})
            return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                "model": "protected.test", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(plan)}}]})

        with OpenAI(api_key="test-only", base_url=ai_config.TAMU_BASE_URL, max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client, patch.dict(
                    "os.environ", {"TAMUS_AI_CHAT_API_KEY": "test-only", "TAMU_MODEL": "protected.test"}, clear=True), patch.object(
                    physical, "_build_client", return_value=client), tempfile.TemporaryDirectory() as temporary:
            result = physical.plan_physical_repair({"available_recipes": []}, diagnostics_path=Path(temporary) / "result.json")
        self.assertEqual(result, plan)
        self.sleep.assert_called_once_with(12)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].content, requests[1].content)
        self.assertTrue(str(requests[0].url).endswith("/openai/chat/completions"))
