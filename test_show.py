"""ci/show and ci/setup-show.sh: "show <command>" for other users on the Pi."""
import getpass
import pathlib
import subprocess

CI = pathlib.Path(__file__).with_name("ci")


def setup(tmp_path, owner):
    """Run setup-show.sh in test mode (no root, no group): files into tmp_path."""
    visudo = tmp_path / "visudo"
    visudo.write_text('#!/bin/sh\necho "$@" > "$(dirname "$0")/visudo.args"\n')
    visudo.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "SHOW_TEST": "1", "SHOW_OWNER": owner,
           "SHOW_BIN": str(tmp_path / "show"), "SHOW_RULE": str(tmp_path / "rule"),
           "VISUDO": str(visudo), "INSTALL_OWNER": ""}
    r = subprocess.run(["sh", str(CI / "setup-show.sh")], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return tmp_path / "show", tmp_path / "rule"


def test_setup_writes_wrapper_and_sudo_rule(tmp_path):
    show, rule = setup(tmp_path, "eduard")
    script = CI.resolve() / "show.sh"
    assert rule.read_text() == f"%slideshow ALL=(eduard) NOPASSWD: {script}\n"
    assert oct(rule.stat().st_mode & 0o777) == "0o440"
    text = show.read_text()
    assert f"SCRIPT={script}\n" in text and "OWNER=eduard\n" in text
    assert "@SCRIPT@" not in text and "@OWNER@" not in text
    assert show.stat().st_mode & 0o111
    # the rule is checked with visudo before it is installed
    assert (tmp_path / "visudo.args").read_text().startswith("-cqf ")


def test_setup_refuses_without_root(tmp_path):
    r = subprocess.run(["sh", str(CI / "setup-show.sh"), "richard"],
                       capture_output=True, text=True)
    if r.returncode == 0:  # running the tests as root
        return
    assert "run as root" in r.stderr


def wrapper(tmp_path, owner):
    """ci/show with a fake show.sh and a fake sudo that log how they ran."""
    fake = tmp_path / "show.sh"
    fake.write_text('#!/bin/sh\necho "show.sh $*"\n')
    fake.chmod(0o755)
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\necho "sudo $*"\n')
    sudo.chmod(0o755)
    w = tmp_path / "show"
    w.write_text((CI / "show").read_text()
                 .replace("@SCRIPT@", str(fake)).replace("@OWNER@", owner))
    w.chmod(0o755)
    return w, {"PATH": "/usr/bin:/bin", "SUDO": str(sudo)}


def test_owner_runs_show_sh_directly(tmp_path):
    w, env = wrapper(tmp_path, getpass.getuser())
    out = subprocess.run([str(w), "status"], env=env, capture_output=True, text=True).stdout
    assert out == "show.sh status\n"


def test_other_users_go_through_sudo_as_the_owner(tmp_path):
    w, env = wrapper(tmp_path, "someone-else")
    out = subprocess.run([str(w), "restart"], env=env, capture_output=True, text=True).stdout
    assert out == f"sudo -n -u someone-else {tmp_path / 'show.sh'} restart\n"
