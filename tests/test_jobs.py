"""Characterization tests for the background-job launcher code generator."""
import base64

import jobs

USER_CODE = 'print("weird \'quotes\'", 1)\nx = """multi\nline"""\n'


class TestJobLaunchCode:
    def test_generated_code_compiles(self):
        code = jobs._job_launch_code("job-abc12345", USER_CODE)
        compile(code, "<job-launch>", "exec")

    def test_user_code_survives_b64_round_trip(self):
        code = jobs._job_launch_code("job-abc12345", USER_CODE)
        b64 = base64.b64encode(USER_CODE.encode()).decode()
        assert b64 in code
        assert base64.b64decode(b64).decode() == USER_CODE

    def test_job_id_in_artifact_paths(self):
        code = jobs._job_launch_code("job-abc12345", "pass")
        assert code.count("'job-abc12345'") >= 4  # .py/.out/.status/.sh + started msg

    def test_uses_kernel_interpreter_and_jobs_dir(self):
        code = jobs._job_launch_code("job-abc12345", "pass")
        assert "sys.executable" in code   # not a hardcoded "python"
        assert jobs._JOBS_DIR in code
        assert "start_new_session=True" in code  # detached from the kernel
