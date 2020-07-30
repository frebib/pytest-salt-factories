"""
    tests.functional.factories.cli.test_ssh
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Test the ``salt-ssh`` CLI functionality
"""
import pathlib


def test_version_info(salt_factories, salt_master_config, salt_version):
    cli = salt_factories.get_salt_ssh_cli(salt_master_config["id"])
    ret = cli.run("--version")
    assert ret.exitcode == 0, ret
    assert ret.stdout.strip() == "{} {}".format(
        pathlib.Path(cli.cli_script_name).name, salt_version
    )
