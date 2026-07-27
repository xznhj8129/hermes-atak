import scripts.install as installer


def test_installer_pins_pymavlink_for_the_gateway_environment():
    assert "pymavlink" in installer.PYMAVLINK_DEPENDENCY
