"""The tester module's live status, faked: shared by the channel page's in-progress tests."""


def running_status(channel_id):
    """channel_tester.get_status() as it reads while `channel_id` is the channel under test."""
    return {
        'is_running': True, 'stop_requested': False,
        'current_channel_id': channel_id, 'current_channel_name': 'Test Channel',
        'current_channel_url': '', 'current_phase': 'testing',
        'current_test_started_at': None, 'current_test_drop_count': 0,
        'current_test_screenshot_url': None, 'current_live_bytes': 0,
        'current_connect_attempt': 1, 'max_connect_attempts': 1,
        'wait_started_at': None, 'wait_duration_seconds': None,
        'last_channel_name': None, 'last_channel_id': None,
        'next_channel_name': None, 'next_channel_id': None,
        'total_channels': 1, 'completed_channels': 0, 'run_started_at': None,
        'last_skip_reason': None, 'current_job_id': None, 'run_kind': 'job',
        'pre_check_recording_id': None, 'logs': [],
    }
