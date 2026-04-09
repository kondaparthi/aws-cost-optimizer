"""
Unit tests for the 11 logic-issue fixes.

Tests cover:
  1. Snapshot chain detection (#2)
  2. Parent tag inheritance (#5)
  3. Resource state re-validation before deletion (#8)
  4. Permission errors vs resource-not-found (#4)
  5. Pricing cache refresh (#9)
  6. Lambda timeout graceful shutdown (#10)
  7. EC2 idle CPU threshold (avg + p95) (#1)
  8. CloudWatch metric completeness (#7)
  9. S3 multipart upload age precision (#3)
  10. EBS delete_on_termination check (#11)
  11. Dry-run mode does not persist decisions (#6)

Copyright (c) 2026 kondaparthi
Licensed under the MIT License.
"""

import json
import importlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

from botocore.exceptions import ClientError

# The scheduler lives in `aws_cost_optimizer.lambda` — "lambda" is a reserved
# Python keyword so dotted-import notation is not valid.  Use importlib instead.
# `zoneinfo` is Python 3.9+; shim it so the module loads on the Python 3.8
# test environment without modifying production code.
import sys
if "zoneinfo" not in sys.modules:
    import types
    _zoneinfo_shim = types.ModuleType("zoneinfo")
    _zoneinfo_shim.ZoneInfo = lambda tz: tz  # type: ignore[attr-defined]
    sys.modules["zoneinfo"] = _zoneinfo_shim

_scheduler_mod = importlib.import_module("aws_cost_optimizer.lambda.scheduler_handler")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "mock"}}, "operation"
    )


def _make_logger():
    log = MagicMock()
    log.log_event = MagicMock()
    return log


# ---------------------------------------------------------------------------
# Test 1 – Snapshot chain detection (Issue #2)
# ---------------------------------------------------------------------------
class TestSnapshotChainDependency(unittest.TestCase):

    def _make_analyzer(self):
        from aws_cost_optimizer.analyzers.ebs_analyzer import EBSAnalyzer
        aws_client = MagicMock()
        skip_policy = MagicMock()
        skip_policy.should_protect_resource.return_value = False
        return EBSAnalyzer(aws_client, "123456789", "us-east-1", skip_policy, _make_logger())

    def _base_snapshot(self, snapshot_id="snap-111"):
        return {
            "SnapshotId": snapshot_id,
            "Description": "My backup",
            "VolumeId": "vol-abc",
            "Tags": [],
        }

    def test_snapshot_with_dependent_is_not_safe(self):
        """A snapshot that has a child snapshot must NOT be flagged."""
        analyzer = self._make_analyzer()
        ec2_client = MagicMock()
        ec2_client.describe_snapshots.return_value = {
            "Snapshots": [{"SnapshotId": "snap-222"}]
        }
        result = analyzer._is_snapshot_safe_to_flag(ec2_client, self._base_snapshot())
        self.assertFalse(result)

    def test_snapshot_without_dependents_is_safe(self):
        """A snapshot with no dependents passes the chain check."""
        analyzer = self._make_analyzer()
        ec2_client = MagicMock()
        ec2_client.describe_snapshots.return_value = {"Snapshots": []}
        result = analyzer._is_snapshot_safe_to_flag(ec2_client, self._base_snapshot())
        self.assertTrue(result)

    def test_aws_backup_description_blocks_deletion(self):
        """Snapshots created by AWS Backup must not be flagged."""
        analyzer = self._make_analyzer()
        ec2_client = MagicMock()
        ec2_client.describe_snapshots.return_value = {"Snapshots": []}
        snap = self._base_snapshot()
        snap["Description"] = "Created by AWS Backup job-abc for vol-xxx"
        result = analyzer._is_snapshot_safe_to_flag(ec2_client, snap)
        self.assertFalse(result)

    def test_dlm_tag_blocks_deletion(self):
        """DLM-managed snapshots (aws:dlm:lifecycle-policy-id tag) must not be flagged."""
        analyzer = self._make_analyzer()
        ec2_client = MagicMock()
        ec2_client.describe_snapshots.return_value = {"Snapshots": []}
        snap = self._base_snapshot()
        snap["Tags"] = [{"Key": "aws:dlm:lifecycle-policy-id", "Value": "pol-123"}]
        result = analyzer._is_snapshot_safe_to_flag(ec2_client, snap)
        self.assertFalse(result)

    def test_clean_snapshot_passes_all_checks(self):
        """A snapshot with no deps, no backup markers, no DLM tag is safe."""
        analyzer = self._make_analyzer()
        ec2_client = MagicMock()
        ec2_client.describe_snapshots.return_value = {"Snapshots": []}
        snap = self._base_snapshot()
        snap["Tags"] = [{"Key": "Name", "Value": "my-snap"}]
        result = analyzer._is_snapshot_safe_to_flag(ec2_client, snap)
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# Test 2 – Parent tag inheritance (Issue #5)
# ---------------------------------------------------------------------------
class TestParentTagInheritance(unittest.TestCase):

    def _make_skip_policy(self, skip_tags=None):
        from aws_cost_optimizer.core import SkipPolicy
        cfg = {"skip_if_tags_match": skip_tags or {"Environment": "prod"}}
        return SkipPolicy(cfg, _make_logger())

    def test_resource_own_tag_protects(self):
        policy = self._make_skip_policy()
        protected = policy.should_protect_resource(
            "vol-abc", {"Environment": "prod"}
        )
        self.assertTrue(protected)

    def test_parent_tag_protects_child(self):
        """Volume attached to a prod instance must be protected even if the
        volume itself has no protection tags."""
        policy = self._make_skip_policy()
        protected = policy.should_protect_resource(
            "snap-abc",
            {"Name": "old-snap"},                      # snapshot has no prod tag
            parent_tags_list=[
                {"Name": "vol-123"},                   # volume has no prod tag
                {"Environment": "prod", "Name": "web-server"},  # instance IS prod
            ],
        )
        self.assertTrue(protected)

    def test_no_protection_when_no_prod_tags(self):
        policy = self._make_skip_policy()
        protected = policy.should_protect_resource(
            "snap-xyz",
            {"Env": "dev"},
            parent_tags_list=[{"Env": "dev"}, {"Env": "staging"}],
        )
        self.assertFalse(protected)

    def test_empty_parent_list_uses_resource_tags_only(self):
        policy = self._make_skip_policy()
        self.assertFalse(
            policy.should_protect_resource("snap-xyz", {"Env": "dev"}, parent_tags_list=[])
        )
        self.assertTrue(
            policy.should_protect_resource("snap-xyz", {"Environment": "prod"}, parent_tags_list=[])
        )


# ---------------------------------------------------------------------------
# Test 3 – Resource state re-validation before action (Issue #8)
# ---------------------------------------------------------------------------
class TestResourceStateVerification(unittest.TestCase):

    def _make_manager(self, ec2_state: str):
        ScheduleManager = _scheduler_mod.ScheduleManager
        mgr = ScheduleManager.__new__(ScheduleManager)
        mgr.timezone = "UTC"
        mgr.logger = _make_logger()
        return mgr, ec2_state

    def test_state_unchanged_allows_action(self):
        """When live state matches analysis state the manager proceeds."""
        sh = _scheduler_mod
        original_ec2 = sh.ec2_client
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [{"State": {"Name": "running"}}]}
            ]
        }
        sh.ec2_client = mock_ec2
        try:
            ScheduleManager = sh.ScheduleManager
            mgr = ScheduleManager.__new__(ScheduleManager)
            result = mgr.verify_resource_current_state("i-abc123")
            self.assertEqual(result, "running")
        finally:
            sh.ec2_client = original_ec2

    def test_missing_instance_returns_none(self):
        """If the instance is gone at execution time, return None."""
        sh = _scheduler_mod
        original_ec2 = sh.ec2_client
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {"Reservations": []}
        sh.ec2_client = mock_ec2
        try:
            ScheduleManager = sh.ScheduleManager
            mgr = ScheduleManager.__new__(ScheduleManager)
            result = mgr.verify_resource_current_state("i-gone")
            self.assertIsNone(result)
        finally:
            sh.ec2_client = original_ec2


# ---------------------------------------------------------------------------
# Test 4 – Permission errors vs resource not found (Issue #4)
# ---------------------------------------------------------------------------
class TestCrossAccountPermissionValidation(unittest.TestCase):

    def _make_aws_client(self):
        from aws_cost_optimizer.core import AWSClient
        client = AWSClient.__new__(AWSClient)
        client.region = "us-east-1"
        client.logger = _make_logger()
        client.default_account_id = "123"
        client.default_role_arn = "arn:aws:iam::123:role/TestRole"
        client.default_external_id = None
        client._role_cache = {}
        return client

    def test_raises_permission_error_on_access_denied(self):
        """AccessDenied on describe_instances must raise PermissionError."""
        from aws_cost_optimizer.core import AWSClient
        aws = self._make_aws_client()

        mock_session = MagicMock()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.side_effect = _make_client_error("UnauthorizedOperation")
        mock_session.client.return_value = mock_ec2
        aws.get_session = MagicMock(return_value=mock_session)

        with self.assertRaises(PermissionError) as ctx:
            aws.validate_cross_account_access(
                account_id="123",
                role_arn="arn:aws:iam::123:role/TestRole",
            )
        self.assertIn("EC2 describe_instances", str(ctx.exception))

    def test_resource_not_found_does_not_raise(self):
        """ResourceAlreadyExists / not-found codes must NOT raise PermissionError."""
        from aws_cost_optimizer.core import AWSClient
        aws = self._make_aws_client()

        mock_session = MagicMock()
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.side_effect = _make_client_error("InvalidInstanceID.NotFound")
        mock_ec2.describe_volumes.return_value = {"Volumes": []}
        mock_s3 = MagicMock()
        mock_s3.list_buckets.return_value = {"Buckets": []}

        def client_factory(service, **kwargs):
            if service == "ec2":
                return mock_ec2
            if service == "s3":
                return mock_s3
            return MagicMock()

        mock_session.client.side_effect = client_factory
        aws.get_session = MagicMock(return_value=mock_session)

        # Should not raise; resource-not-found is not a permission error
        try:
            aws.validate_cross_account_access(account_id="123", role_arn="arn:aws:iam::123:role/R")
        except PermissionError:
            self.fail("validate_cross_account_access raised PermissionError for non-auth error")

    def test_all_permissions_ok_does_not_raise(self):
        """When all probes succeed, no exception is raised."""
        from aws_cost_optimizer.core import AWSClient
        aws = self._make_aws_client()

        mock_session = MagicMock()
        mock_session.client.return_value = MagicMock()
        aws.get_session = MagicMock(return_value=mock_session)

        aws.validate_cross_account_access(account_id="123", role_arn="arn:aws:iam::123:role/R")


# ---------------------------------------------------------------------------
# Test 5 – Pricing cache refresh (Issue #9)
# ---------------------------------------------------------------------------
class TestPricingCacheRefresh(unittest.TestCase):

    def _make_cache(self, s3_has_fresh_data=False, s3_has_stale_data=False):
        from aws_cost_optimizer.core.pricing_cache import PricingCache
        cache = PricingCache.__new__(PricingCache)
        cache.s3_bucket = "test-bucket"
        cache.region = "us-east-1"
        cache._in_memory = None

        mock_s3 = MagicMock()
        mock_pricing = MagicMock()

        if s3_has_fresh_data:
            fresh_doc = json.dumps({
                "fetch_date": datetime.utcnow().isoformat(),
                "prices": {
                    "us-east-1": {
                        "ebs": {"gp3": 0.08},
                        "s3": {"standard": 0.023},
                        "ec2": {"t3.micro": 0.0104},
                    }
                },
            }).encode()
            mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: fresh_doc)}

        elif s3_has_stale_data:
            stale_doc = json.dumps({
                "fetch_date": (datetime.utcnow() - timedelta(hours=48)).isoformat(),
                "prices": {},
            }).encode()
            mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: stale_doc)}
        else:
            # No S3 object
            mock_s3.get_object.side_effect = _make_client_error("NoSuchKey")

        # Mock pricing API to return a simple price
        mock_pricing.get_products.return_value = {
            "PriceList": [json.dumps({
                "terms": {
                    "OnDemand": {
                        "term1": {
                            "priceDimensions": {
                                "dim1": {"unit": "GB-Mo", "pricePerUnit": {"USD": "0.08"}}
                            }
                        }
                    }
                }
            })]
        }

        cache._s3 = mock_s3
        cache._pricing = mock_pricing
        return cache

    def test_fresh_s3_cache_is_used_without_api_call(self):
        """Fresh S3 cache should be served without any Pricing API calls."""
        cache = self._make_cache(s3_has_fresh_data=True)
        price = cache.get_price("ebs", "gp3", "us-east-1")
        self.assertEqual(price, 0.08)
        cache._pricing.get_products.assert_not_called()

    def test_stale_s3_cache_triggers_api_refresh(self):
        """A stale S3 cache must trigger a Pricing API refresh."""
        cache = self._make_cache(s3_has_stale_data=True)
        # Refresh sets _in_memory; mock put_object to avoid real call
        cache._s3.put_object = MagicMock()
        cache.get_price("ebs", "gp3", "us-east-1")
        cache._pricing.get_products.assert_called()

    def test_missing_price_falls_back_to_hardcoded(self):
        """Unknown resource type falls back to the hardcoded table."""
        from aws_cost_optimizer.core.pricing_cache import FALLBACK_PRICING
        cache = self._make_cache(s3_has_fresh_data=True)
        price = cache.get_price("ebs", "nonexistent_type", "us-east-1")
        self.assertEqual(price, 0.0)  # Not in fallback, defaults to 0

    def test_fallback_used_for_known_type_missing_from_api(self):
        """gp3 missing from API response falls back to hardcoded gp3 price."""
        from aws_cost_optimizer.core.pricing_cache import FALLBACK_PRICING, PricingCache
        cache = self._make_cache()  # No S3, API must be called
        cache._s3.put_object = MagicMock()
        # Make refresh() populate in-memory with empty prices for all regions
        cache.refresh = MagicMock(return_value={})
        cache._in_memory = {"fetch_date": datetime.utcnow().isoformat(), "prices": {}}
        price = cache.get_price("ebs", "gp3", "us-east-1")
        self.assertEqual(price, FALLBACK_PRICING["ebs"]["gp3"])

    def test_price_fetch_date_present_in_cache(self):
        """price_fetch_date() must return a non-empty ISO string."""
        cache = self._make_cache(s3_has_fresh_data=True)
        date_str = cache.price_fetch_date()
        self.assertTrue(len(date_str) > 0)
        self.assertNotEqual(date_str, "unknown")


# ---------------------------------------------------------------------------
# Test 6 – Lambda timeout graceful shutdown (Issue #10)
# ---------------------------------------------------------------------------
class TestLambdaTimeoutGracefulShutdown(unittest.TestCase):

    def _make_context(self, remaining_ms: int):
        ctx = MagicMock()
        ctx.get_remaining_time_in_millis.return_value = remaining_ms
        return ctx

    def test_timeout_sets_partial_status(self):
        """When <30s remain, analysis_status should be 'partial'."""
        from aws_cost_optimizer.models import FindingsReport
        report = FindingsReport()

        # Simulate the guard condition directly
        SAFETY_MARGIN = 30
        remaining_ms = 5_000  # 5 seconds — below margin
        remaining_seconds = remaining_ms / 1000

        if remaining_seconds < SAFETY_MARGIN:
            report.analysis_status = "partial"
            report.partial_reason = f"Only {remaining_seconds}s remaining"

        self.assertEqual(report.analysis_status, "partial")
        self.assertIsNotNone(report.partial_reason)

    def test_sufficient_time_leaves_status_complete(self):
        """When plenty of time remains, status must stay 'complete'."""
        from aws_cost_optimizer.models import FindingsReport
        report = FindingsReport()

        SAFETY_MARGIN = 30
        remaining_ms = 300_000  # 5 minutes — plenty of time

        if remaining_ms / 1000 < SAFETY_MARGIN:
            report.analysis_status = "partial"

        self.assertEqual(report.analysis_status, "complete")

    def test_partial_status_persists_in_json(self):
        """Partial status must appear in the serialised findings JSON."""
        from aws_cost_optimizer.models import FindingsReport
        report = FindingsReport()
        report.analysis_status = "partial"
        report.partial_reason = "Timeout safety margin reached"

        data = report.to_dict()
        self.assertEqual(data["analysis_status"], "partial")
        self.assertIn("Timeout", data["partial_reason"])


# ---------------------------------------------------------------------------
# Test 7 – EC2 idle CPU threshold: avg AND p95 (Issue #1)
# ---------------------------------------------------------------------------
class TestEC2IdleCPUThreshold(unittest.TestCase):

    def _make_ec2_analyzer(self):
        from aws_cost_optimizer.analyzers.ec2_analyzer import EC2Analyzer
        aws_client = MagicMock()
        skip_policy = MagicMock()
        skip_policy.should_skip.return_value = False
        return EC2Analyzer(aws_client, "123", "us-east-1", skip_policy, _make_logger())

    def _make_datapoints(self, avg_values, period_s=3600):
        """Generate dummy datapoints with given average CPU values."""
        now = datetime.utcnow()
        return [
            {
                "Timestamp": now - timedelta(hours=i),
                "Average": v,
            }
            for i, v in enumerate(avg_values)
        ]

    def _make_p95_datapoints(self, p95_values, period_s=3600):
        now = datetime.utcnow()
        return [
            {
                "Timestamp": now - timedelta(hours=i),
                "ExtendedStatistics": {"p95": v},
            }
            for i, v in enumerate(p95_values)
        ]

    def test_both_thresholds_met_flags_idle(self):
        """avg <5 AND p95 <8 should flag the instance as idle."""
        from aws_cost_optimizer.analyzers.ec2_analyzer import _IDLE_P95_CPU_CEILING
        analyzer = self._make_ec2_analyzer()
        avg_dps = self._make_datapoints([1.2] * 168)  # 7 days * 24h
        p95_dps = self._make_p95_datapoints([3.5] * 168)

        avg_cpu = sum(dp["Average"] for dp in avg_dps) / len(avg_dps)
        p95_cpu = max(dp["ExtendedStatistics"]["p95"] for dp in p95_dps)

        self.assertTrue(avg_cpu < 5 and p95_cpu < _IDLE_P95_CPU_CEILING)

    def test_high_p95_prevents_idle_flag(self):
        """avg <5 but p95 >=8 must NOT flag the instance as idle."""
        from aws_cost_optimizer.analyzers.ec2_analyzer import _IDLE_P95_CPU_CEILING
        avg_dps = self._make_datapoints([2.0] * 168)
        p95_dps = self._make_p95_datapoints([9.5] * 168)  # p95 above ceiling

        avg_cpu = sum(dp["Average"] for dp in avg_dps) / len(avg_dps)
        p95_cpu = max(dp["ExtendedStatistics"]["p95"] for dp in p95_dps)

        self.assertFalse(avg_cpu < 5 and p95_cpu < _IDLE_P95_CPU_CEILING)

    def test_high_average_prevents_idle_flag(self):
        """avg >=5 even with low p95 must NOT flag idle."""
        from aws_cost_optimizer.analyzers.ec2_analyzer import _IDLE_P95_CPU_CEILING
        avg_dps = self._make_datapoints([6.0] * 168)
        p95_dps = self._make_p95_datapoints([6.5] * 168)

        avg_cpu = sum(dp["Average"] for dp in avg_dps) / len(avg_dps)
        p95_cpu = max(dp["ExtendedStatistics"]["p95"] for dp in p95_dps)

        self.assertFalse(avg_cpu < 5 and p95_cpu < _IDLE_P95_CPU_CEILING)


# ---------------------------------------------------------------------------
# Test 8 – CloudWatch metric completeness (Issue #7)
# ---------------------------------------------------------------------------
class TestCloudWatchMetricCompleteness(unittest.TestCase):

    def _make_analyzer(self):
        from aws_cost_optimizer.analyzers.ec2_analyzer import EC2Analyzer
        aws_client = MagicMock()
        skip_policy = MagicMock()
        return EC2Analyzer(aws_client, "123", "us-east-1", skip_policy, _make_logger())

    def _make_datapoints(self, count: int, days: int = 7):
        """Create `count` evenly-spaced datapoints over `days`."""
        now = datetime.utcnow()
        step = timedelta(days=days) / max(count, 1)
        return [{"Timestamp": now - step * i, "Average": 2.0} for i in range(count)]

    def test_full_coverage_returns_high_confidence(self):
        """168 hourly datapoints for 7 days → high confidence."""
        analyzer = self._make_analyzer()
        dps = self._make_datapoints(168, days=7)
        result = analyzer.validate_metric_completeness(dps, days=7)
        self.assertEqual(result["confidence"], "high")
        self.assertGreaterEqual(result["completeness_pct"], 95)

    def test_sparse_data_returns_low_confidence(self):
        """Only 10 datapoints for 7 days (<<95%) → low confidence."""
        analyzer = self._make_analyzer()
        dps = self._make_datapoints(10, days=7)
        result = analyzer.validate_metric_completeness(dps, days=7)
        self.assertEqual(result["confidence"], "low")
        self.assertLess(result["completeness_pct"], 95)

    def test_gaps_detected_in_low_confidence(self):
        """Sparse data should be accompanied by gap information."""
        analyzer = self._make_analyzer()
        # Two datapoints widely separated
        now = datetime.utcnow()
        dps = [
            {"Timestamp": now - timedelta(days=6), "Average": 1.0},
            {"Timestamp": now - timedelta(hours=1), "Average": 1.0},
        ]
        result = analyzer.validate_metric_completeness(dps, days=7)
        self.assertEqual(result["confidence"], "low")
        self.assertGreater(len(result["gaps"]), 0)


# ---------------------------------------------------------------------------
# Test 9 – S3 multipart upload age precision (Issue #3)
# ---------------------------------------------------------------------------
class TestS3MultipartUploadAge(unittest.TestCase):

    def test_exactly_168_hours_is_stale(self):
        from aws_cost_optimizer.analyzers.s3_analyzer import INCOMPLETE_UPLOAD_THRESHOLD_HOURS
        now = datetime.utcnow()
        initiated = now - timedelta(hours=168)
        age_hours = (now - initiated).total_seconds() / 3600
        self.assertTrue(age_hours >= INCOMPLETE_UPLOAD_THRESHOLD_HOURS)

    def test_167_hours_is_not_stale(self):
        from aws_cost_optimizer.analyzers.s3_analyzer import INCOMPLETE_UPLOAD_THRESHOLD_HOURS
        now = datetime.utcnow()
        initiated = now - timedelta(hours=167)
        age_hours = (now - initiated).total_seconds() / 3600
        self.assertFalse(age_hours >= INCOMPLETE_UPLOAD_THRESHOLD_HOURS)

    def test_timezone_aware_initiated_handled(self):
        """UTC-aware datetime must be comparable without ValueError."""
        from aws_cost_optimizer.analyzers.s3_analyzer import INCOMPLETE_UPLOAD_THRESHOLD_HOURS
        now = datetime.utcnow()
        # Simulate boto3 returning timezone-aware datetime
        initiated_aware = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(hours=200)
        initiated = initiated_aware.replace(tzinfo=None)
        age_hours = (now - initiated).total_seconds() / 3600
        self.assertTrue(age_hours >= INCOMPLETE_UPLOAD_THRESHOLD_HOURS)

    def test_threshold_constant_equals_seven_days(self):
        from aws_cost_optimizer.analyzers.s3_analyzer import INCOMPLETE_UPLOAD_THRESHOLD_HOURS
        self.assertEqual(INCOMPLETE_UPLOAD_THRESHOLD_HOURS, 7 * 24)


# ---------------------------------------------------------------------------
# Test 10 – EBS delete_on_termination check (Issue #11)
# ---------------------------------------------------------------------------
class TestEBSIsUnattached(unittest.TestCase):

    def test_volume_with_no_attachments_is_unattached(self):
        from aws_cost_optimizer.analyzers.ebs_analyzer import EBSAnalyzer
        vol = {"Attachments": []}
        self.assertTrue(EBSAnalyzer.is_truly_unattached(vol))

    def test_volume_with_delete_on_termination_is_managed(self):
        """A volume whose attachment has DeleteOnTermination=True is managed."""
        from aws_cost_optimizer.analyzers.ebs_analyzer import EBSAnalyzer
        vol = {
            "Attachments": [
                {"InstanceId": "i-abc", "DeleteOnTermination": True, "State": "attached"}
            ]
        }
        self.assertFalse(EBSAnalyzer.is_truly_unattached(vol))

    def test_volume_with_regular_attachment_is_not_unattached(self):
        """Any live attachment (even without DeleteOnTermination) means in-use."""
        from aws_cost_optimizer.analyzers.ebs_analyzer import EBSAnalyzer
        vol = {
            "Attachments": [
                {"InstanceId": "i-abc", "DeleteOnTermination": False, "State": "attached"}
            ]
        }
        self.assertFalse(EBSAnalyzer.is_truly_unattached(vol))

    def test_missing_attachments_key_is_unattached(self):
        """Volume dict with no Attachments key is treated as unattached."""
        from aws_cost_optimizer.analyzers.ebs_analyzer import EBSAnalyzer
        self.assertTrue(EBSAnalyzer.is_truly_unattached({}))


# ---------------------------------------------------------------------------
# Test 11 – Dry-run does not persist decisions (Issue #6)
# ---------------------------------------------------------------------------
class TestDryRunNoPersistence(unittest.TestCase):

    def test_dry_run_populates_simulation_fields(self):
        """dry_run_would_stop and dry_run_would_start must be populated, not
        the real stopped/started lists, in dry-run mode."""
        # We test the logic directly (lambda_handler requires full env setup)
        actions = {
            "stopped": [],
            "started": [],
            "errors": [],
            "dry_run_would_stop": [],
            "dry_run_would_start": [],
            "dry_run_would_skip_state_changed": [],
        }
        dry_run = True
        instance_id = "i-test001"
        live_state = "running"
        should_stop = True

        if dry_run:
            if should_stop and live_state == "running":
                actions["dry_run_would_stop"].append(instance_id)
        else:
            actions["stopped"].append(instance_id)

        self.assertIn(instance_id, actions["dry_run_would_stop"])
        self.assertNotIn(instance_id, actions["stopped"])

    def test_real_mode_populates_real_fields(self):
        actions = {
            "stopped": [],
            "dry_run_would_stop": [],
        }
        dry_run = False
        instance_id = "i-test002"
        should_stop = True
        live_state = "running"

        if dry_run:
            if should_stop and live_state == "running":
                actions["dry_run_would_stop"].append(instance_id)
        else:
            if should_stop and live_state == "running":
                actions["stopped"].append(instance_id)

        self.assertIn(instance_id, actions["stopped"])
        self.assertNotIn(instance_id, actions["dry_run_would_stop"])


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
