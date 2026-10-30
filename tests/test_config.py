from total_orch.config import Settings


def test_redacted_urls_remove_userinfo_query_and_fragment() -> None:
    settings = Settings(
        gateway_base_url="https://user:secret@example.test:8443/api?token=query-secret#fragment",
        omniroute_base_url="https://omni.test/?key=omni-secret",
    )

    redacted = settings.redacted()

    assert redacted["gateway_base_url"] == "https://example.test:8443/api"
    assert redacted["omniroute_base_url"] == "https://omni.test/"
    assert "secret" not in repr(redacted)


def test_redacted_urls_keep_normal_components() -> None:
    settings = Settings(
        gateway_base_url="http://router.test:8080/api",
        modelsdev_base_url="https://models.dev/catalog",
    )

    redacted = settings.redacted()

    assert redacted["gateway_base_url"] == "http://router.test:8080/api"
    assert redacted["modelsdev_base_url"] == "https://models.dev/catalog"
