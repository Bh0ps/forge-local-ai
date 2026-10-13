"""Native Inno smoke tests use inert files, no registry, shortcuts or app launch."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import subprocess
import sys
import shutil
import time
import uuid

import pytest

SOURCE = Path(__file__).resolve().parents[1]
SCRIPT = SOURCE / "installer.iss"
ISCC = Path(os.environ.get("FORGE_TEST_ISCC", str(SOURCE.parent / "forge-5.0-build-tools/compiler/ISCC.exe")))


def test_public_installer_never_uses_wildcard_deletion_or_process_closure():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "[InstallDelete]" not in text and "DelTree(" not in text
    assert re.search(r"^CloseApplications=no$", text, re.M)
    assert re.search(r"^UsePreviousAppDir=no$", text, re.M)
    assert "taskkill" not in text.casefold() and "TerminateProcess" not in text


@pytest.fixture
def installer(tmp_path):
    if os.name != "nt" or not ISCC.is_file():
        pytest.skip("The native Windows Inno compiler is not available.")
    base, stage = tmp_path / "profile", tmp_path / "stage"
    base.mkdir(); stage.mkdir()
    (stage / "Forge.exe").write_bytes(b"Unexecuted synthetic new Forge")
    (stage / "ForgeBrowserHost.exe").write_bytes(b"Unexecuted synthetic new browser")
    nested = stage / "_internal/frontend/dist/assets"
    nested.mkdir(parents=True)
    (nested / "new-chunk.js").write_bytes(b"Synthetic new chunk")
    app = base / "Programs/Forge5"
    backup = base / "ForgeBackups"

    def compile(failure=False, transform=None):
        # Change only compiled path constants and disable installer side effects.
        # The actual production guard/move/restore implementation runs unchanged.
        text = SCRIPT.read_text(encoding="utf-8")
        text = re.sub(r"^AppId=.*$", "AppId=" + str(uuid.uuid4()), text, flags=re.M)
        text = re.sub(r"^SetupMutex=.*$", "SetupMutex=Forge.Disposable." + uuid.uuid4().hex, text, flags=re.M)
        text = re.sub(r"^DefaultDirName=.*$", lambda _: "DefaultDirName=" + str(app), text, flags=re.M)
        text = re.sub(r"^LicenseFile=.*$", lambda _: "LicenseFile=" + str(SOURCE / "LICENSE"), text, flags=re.M)
        text = re.sub(r"^SetupIconFile=.*$", lambda _: "SetupIconFile=" + str(SOURCE / "assets/forge.ico"), text, flags=re.M)
        text = text.replace("Result := ExpandConstant('{localappdata}');", "Result := '" + str(base).replace("'", "''") + "';")
        text = text.replace("[Setup]\n", "[Setup]\nUninstallable=no\nCreateUninstallRegKey=no\n")
        text = re.sub(r"\[Icons\].*?(?=\[Code\])", "", text, flags=re.S)
        if failure:
            # A real extraction error after the first file: block the next
            # destination with a directory. A post-install script exception is
            # only logged by Inno and is not an actual failed installation.
            text = text.replace("Flags: ignoreversion recursesubdirs createallsubdirs", "Flags: ignoreversion recursesubdirs createallsubdirs; BeforeInstall: FixtureExtractionFailure")
            text = text.replace("[Code]\n", "[Code]\nprocedure FixtureExtractionFailure;\nbegin\n  SaveStringToFile(ExpandConstant('{app}\\unfinished-note.txt'), 'Synthetic partial fixture', False);\n  CreateDir(ExpandConstant('{app}\\ForgeBrowserHost.exe'));\nend;\n")
        if transform:
            text = transform(text)
        fixture_id = uuid.uuid4().hex
        script = tmp_path / (fixture_id + ".iss")
        script.write_text(text, encoding="utf-8")
        output = tmp_path / (fixture_id + "-package")
        result = subprocess.run([str(ISCC), "/Q", "/DStageDir=" + str(stage), "/O" + str(output), str(script)],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stdout + result.stderr
        return output / "Forge-5.1.2-Setup.exe"

    return base, app, backup, stage, compile


def execute(package, target, tmp_path):
    log = tmp_path / ("setup-" + uuid.uuid4().hex + ".log")
    result = subprocess.run([str(package), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        "/NOCLOSEAPPLICATIONS", "/DIR=" + str(target), "/LOG=" + str(log)],
        capture_output=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
    return result.returncode, log.read_text(encoding="utf-8-sig")


def seed_old(app):
    app.mkdir(parents=True)
    (app / "Forge.exe").write_bytes(b"Synthetic previous Forge")
    (app / "unknown-note.txt").write_bytes(b"Synthetic personal fixture, preserve all bytes")
    assets = app / "_internal/frontend/dist/assets"
    assets.mkdir(parents=True)
    (assets / "old-chunk.js").write_bytes(b"Synthetic old chunk")
    return {path.relative_to(app).as_posix(): path.read_bytes() for path in app.rglob("*") if path.is_file()}


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_existing_preview_is_preserved_whole_and_fresh_tree_has_no_obsolete_files(installer, tmp_path):
    base, app, backup, stage, compile = installer
    old = seed_old(app)
    state = base / ".forge/state"; state.mkdir(parents=True)
    (state / "fixture.sqlite3").write_bytes(b"Synthetic state untouched")
    models = base / "models"; models.mkdir()
    (models / "fixture.gguf").write_bytes(b"Synthetic model untouched")
    code, log = execute(compile(), app, tmp_path)
    assert code == 0, log
    assert files(app) == files(stage)
    preserved = list(backup.glob("Forge5-previous-*"))
    assert len(preserved) == 1 and files(preserved[0]) == old
    assert (state / "fixture.sqlite3").read_bytes() == b"Synthetic state untouched"
    assert (models / "fixture.gguf").read_bytes() == b"Synthetic model untouched"


def test_fresh_default_install_and_wrong_custom_target(installer, tmp_path):
    _, app, backup, stage, compile = installer
    package = compile()
    custom = tmp_path / "custom"
    code, _ = execute(package, custom, tmp_path)
    assert code != 0 and not custom.exists() and not app.exists() and not backup.exists()
    code, log = execute(package, app, tmp_path)
    assert code == 0, log
    assert files(app) == files(stage) and not backup.exists()


def test_locked_previous_file_aborts_without_backup_or_overwrite(installer, tmp_path):
    _, app, backup, _, compile = installer
    old = seed_old(app)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(app / "Forge.exe"), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        code, log = execute(compile(), app, tmp_path)
    finally:
        kernel.CloseHandle(handle)
    assert code != 0, log
    assert files(app) == old and not backup.exists()


def test_failed_install_restores_old_tree_and_retains_partial_new_tree(installer, tmp_path):
    _, app, backup, _, compile = installer
    old = seed_old(app)
    code, log = execute(compile(failure=True), app, tmp_path)
    assert code != 0, log
    assert files(app) == old
    retained = list(backup.glob("Forge5-failed-*"))
    assert len(retained) == 1
    assert (retained[0] / "unfinished-note.txt").read_bytes() == b"Synthetic partial fixture"
    assert "Forge.exe" in log and "restored after installation failed" in log


@pytest.mark.parametrize("hidden_image", [None, "Unassigned", "Null", "''"])
def test_actual_running_image_blocks_upgrade_without_killing_it(installer, tmp_path, hidden_image):
    _, app, backup, _, compile = installer
    old = seed_old(app)
    runtime = Path(sys._base_executable).resolve()
    shutil.copy2(runtime, app / "Forge.exe")
    old["Forge.exe"] = (app / "Forge.exe").read_bytes()
    package = compile(transform=(lambda text: text.replace("Image := Item.ExecutablePath;", "Image := " + hidden_image + ";")) if hidden_image is not None else None)
    process = subprocess.Popen([str(app / "Forge.exe"), "-c", "import sys; print('fixture-ready',flush=True); sys.stdin.readline()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONHOME": str(runtime.parent), "PATH": str(runtime.parent) + os.pathsep + os.environ["PATH"]},
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert process.stdout.readline().strip() == "fixture-ready"
        code, log = execute(package, app, tmp_path)
        assert code != 0, log
        assert process.poll() is None
        assert files(app) == old and not backup.exists()
    finally:
        process.communicate(input="\n", timeout=10)
    assert process.returncode == 0


@pytest.mark.parametrize("image_name", ["Forge.exe", "ForgeBrowserHost.exe", "worker.exe"])
def test_actual_8dot3_image_path_blocks_upgrade(installer, tmp_path, image_name):
    _, app, backup, _, compile = installer
    old = seed_old(app)
    runtime = Path(sys._base_executable).resolve()
    executable = app / image_name
    shutil.copy2(runtime, executable)
    old[image_name] = executable.read_bytes()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetShortPathNameW.restype = wintypes.DWORD
    short = ctypes.create_unicode_buffer(32768)
    length = kernel.GetShortPathNameW(str(executable), short, len(short))
    if not length or length >= len(short) or short.value.casefold() == str(executable).casefold():
        pytest.skip("This disposable NTFS tree has no short-path alias; system settings are never changed.")
    package = compile()
    host = subprocess.Popen([short.value, "-c", "import sys; print('fixture-ready',flush=True); sys.stdin.readline()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONHOME": str(runtime.parent), "PATH": str(runtime.parent) + os.pathsep + os.environ["PATH"]},
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert host.stdout.readline().strip() == "fixture-ready"
        code, log = execute(package, app, tmp_path)
        assert code != 0, log
        assert host.poll() is None
        assert files(app) == old and not backup.exists()
    finally:
        host.communicate(input="\n", timeout=10)
    assert host.returncode == 0


def test_renamed_running_worker_inside_app_blocks_upgrade(installer, tmp_path):
    _, app, backup, _, compile = installer
    old = seed_old(app)
    runtime = Path(sys._base_executable).resolve()
    executable = app / "worker.exe"
    shutil.copy2(runtime, executable)
    old["worker.exe"] = executable.read_bytes()
    package = compile()
    host = subprocess.Popen([str(executable), "-c", "import sys; print('fixture-ready',flush=True); sys.stdin.readline()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONHOME": str(runtime.parent), "PATH": str(runtime.parent) + os.pathsep + os.environ["PATH"]},
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert host.stdout.readline().strip() == "fixture-ready"
        code, log = execute(package, app, tmp_path)
        assert code != 0, log
        assert host.poll() is None
        assert files(app) == old and not backup.exists()
    finally:
        host.communicate(input="\n", timeout=10)
    assert host.returncode == 0


@pytest.mark.parametrize("location", ["app", "programs", "backup", "nested"])
def test_junction_targets_are_refused_without_touching_external_files(installer, tmp_path, location):
    base, app, backup, _, compile = installer
    package = compile()
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_bytes(b"Synthetic external sentinel")
    if location == "app":
        app.parent.mkdir(parents=True)
        link = app
        old = None
    elif location == "programs":
        link = app.parent
        old = None
    else:
        old = seed_old(app)
        link = backup if location == "backup" else app / "nested-link"
    result = subprocess.run(["powershell", "-NoProfile", "-Command",
        "New-Item -ItemType Junction -Path $env:FORGE_FIXTURE_LINK -Target $env:FORGE_FIXTURE_TARGET | Out-Null"],
        env={**os.environ, "FORGE_FIXTURE_LINK": str(link), "FORGE_FIXTURE_TARGET": str(external)},
        capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    try:
        code, log = execute(package, app, tmp_path)
        assert code != 0, log
        assert files(external) == {"sentinel.txt": b"Synthetic external sentinel"}
        assert link.is_junction()
    finally:
        # rmdir removes only the junction entry. Never recurse into its target.
        link.rmdir()
    if old is not None:
        assert files(app) == old
    else:
        assert not app.exists()
    assert not backup.exists()


def test_cancel_during_extraction_restores_old_and_retains_new_unknown_files(installer, tmp_path):
    _, app, backup, _, compile = installer
    old = seed_old(app)

    def cancel_fixture(text):
        text = text.replace("Flags: ignoreversion recursesubdirs createallsubdirs", "Flags: ignoreversion recursesubdirs createallsubdirs; AfterInstall: FixtureCancel")
        callback = """procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  Cancel := True;
  Confirm := False;
end;
procedure FixtureCancel;
begin
  SaveStringToFile(ExpandConstant('{app}\\cancelled-note.txt'), 'Synthetic cancellation fixture', False);
  // Inno accepts user cancellation only when its wizard is visible/enabled.
  // Exercise that engine path offscreen without showing an interactive window.
  WizardForm.Left := -32000;
  WizardForm.Top := -32000;
  WizardForm.Visible := True;
  WizardForm.CancelButton.OnClick(WizardForm.CancelButton);
end;
"""
        return text.replace("[Code]\n", "[Code]\n" + callback)

    code, log = execute(compile(transform=cancel_fixture), app, tmp_path)
    assert code != 0, log
    assert files(app) == old
    retained = list(backup.glob("Forge5-failed-*"))
    assert len(retained) == 1
    assert (retained[0] / "cancelled-note.txt").read_bytes() == b"Synthetic cancellation fixture"


@pytest.mark.parametrize("boundary,image_name,short_launch", [
    ("released", "Forge.exe", False), ("moved", "Forge.exe", False), ("guarded", "Forge.exe", False),
    ("released", "ForgeBrowserHost.exe", True), ("moved", "ForgeBrowserHost.exe", True),
])
def test_late_process_admission_cannot_overlap_extraction(installer, tmp_path, boundary, image_name, short_launch):
    _, app, backup, stage, compile = installer
    old = seed_old(app)
    runtime = Path(sys._base_executable).resolve()
    shutil.copy2(runtime, app / image_name)
    old[image_name] = (app / image_name).read_bytes()
    marker = tmp_path / "boundary.txt"
    refused = tmp_path / "refused.txt"
    needle = {
        "released": "      if not RenameFile(AppRoot, PreviousRoot) then begin",
        "moved": "      PreviousMoved := True;",
        "guarded": "      KeepPreviousGuards := True;",
    }[boundary]
    pause = "\n      SaveStringToFile('" + str(marker).replace("'", "''") + "', 'Synthetic boundary', False);\n      Sleep(3000);\n"

    def boundary_fixture(text):
        assert text.count(needle) == 1
        text = text.replace(needle, pause + needle if boundary == "released" else needle + pause)
        refusal = "      if not HoldTree(PreviousRoot, 0) or not PreviousHostsStopped then begin"
        return text.replace(refusal, refusal + "\n        SaveStringToFile('" + str(refused).replace("'", "''") + "', 'Synthetic refusal', False);\n        Sleep(3000);\n")

    package = compile(transform=boundary_fixture)
    log = tmp_path / "boundary-setup.log"
    setup = subprocess.Popen([str(package), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        "/NOCLOSEAPPLICATIONS", "/DIR=" + str(app), "/LOG=" + str(log)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    host = None
    try:
        until = time.monotonic() + 90
        while not marker.exists() and time.monotonic() < until and setup.poll() is None:
            time.sleep(.02)
        assert marker.exists(), log.read_text(encoding="utf-8-sig") if log.exists() else "Fixture did not reach boundary"
        executable = app / image_name if boundary == "released" else next(backup.glob("Forge5-previous-*")) / image_name
        launch_path = str(executable)
        if short_launch:
            short = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetShortPathNameW(launch_path, short, len(short))
            assert 0 < length < len(short)
            # Existing aliases are used; no 8dot3 volume setting is changed.
            launch_path = short.value
        args = [launch_path, "-c", "import sys; print('fixture-ready',flush=True); sys.stdin.readline()"]
        env = {**os.environ, "PYTHONHOME": str(runtime.parent), "PATH": str(runtime.parent) + os.pathsep + os.environ["PATH"]}
        if boundary == "guarded":
            with pytest.raises(PermissionError):
                host = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            host = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            assert host.stdout.readline().strip() == "fixture-ready"
            until = time.monotonic() + 90
            while not refused.exists() and time.monotonic() < until and setup.poll() is None:
                time.sleep(.02)
            assert refused.exists()
            assert host.poll() is None
            host.communicate(input="\n", timeout=10)
            assert host.returncode == 0
            host = None
        setup.communicate(timeout=90)
        output = log.read_text(encoding="utf-8-sig")
        if boundary == "guarded":
            assert setup.returncode == 0, output
            assert files(app) == files(stage)
            assert files(next(backup.glob("Forge5-previous-*"))) == old
        else:
            assert setup.returncode != 0, output
            assert "Starting the installation process" not in output
            assert files(app) == old
            assert not list(backup.glob("Forge5-previous-*"))
    finally:
        if host is not None:
            host.communicate(input="\n", timeout=10)
        setup.communicate(timeout=90)


def test_host_starting_during_failed_install_recovery_keeps_both_trees(installer, tmp_path):
    _, app, backup, _, compile = installer
    old = seed_old(app)
    runtime = Path(sys._base_executable).resolve()
    shutil.copy2(runtime, app / "Forge.exe")
    old["Forge.exe"] = (app / "Forge.exe").read_bytes()
    marker = tmp_path / "recovery-boundary.txt"

    def recovery_fixture(text):
        needle = "  if not PreviousMoved or InstallCompleted then Exit;"
        assert text.count(needle) == 1
        pause = "  SaveStringToFile('" + str(marker).replace("'", "''") + "', 'Synthetic recovery boundary', False);\n  Sleep(3000);\n"
        return text.replace(needle, pause + needle)

    package = compile(failure=True, transform=recovery_fixture)
    log = tmp_path / "recovery-setup.log"
    setup = subprocess.Popen([str(package), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        "/NOCLOSEAPPLICATIONS", "/DIR=" + str(app), "/LOG=" + str(log)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    host = None
    try:
        until = time.monotonic() + 90
        while not marker.exists() and time.monotonic() < until and setup.poll() is None:
            time.sleep(.02)
        assert marker.exists()
        previous = next(backup.glob("Forge5-previous-*"))
        host = subprocess.Popen([str(previous / "Forge.exe"), "-c", "import sys; print('fixture-ready',flush=True); sys.stdin.readline()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONHOME": str(runtime.parent), "PATH": str(runtime.parent) + os.pathsep + os.environ["PATH"]},
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert host.stdout.readline().strip() == "fixture-ready"
        setup.communicate(timeout=90)
        assert setup.returncode != 0
        assert host.poll() is None
        assert files(previous) == old
        assert (app / "unfinished-note.txt").read_bytes() == b"Synthetic partial fixture"
        assert not list(backup.glob("Forge5-failed-*"))
        assert "recovery is waiting" in log.read_text(encoding="utf-8-sig")
    finally:
        if host is not None:
            host.communicate(input="\n", timeout=10)
        setup.communicate(timeout=90)
