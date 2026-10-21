"""Tests for the Revocación fork (--whatsapp-format)."""
import hashlib

from Whatsapp_Chat_Exporter.revocacion import MediaRef, number_like, phone_of


def test_number_like_formatted_phone_names():
    assert number_like("‪+34 600 36 25 81‬")
    assert number_like("+1 (313) 555-0002")
    assert number_like("‪+972 50‑907‑9786‬")
    assert not number_like("Julia")
    assert not number_like("Cuarto 7 Mar 14")
    assert not number_like(None)


def test_phone_of():
    assert phone_of("34616014470@s.whatsapp.net") == "+34616014470"
    assert phone_of("120363163113098798@g.us") is None
    assert phone_of("43770162765827@lid") is None


def test_media_ref_modern_backup():
    ref = MediaRef("AppDomainGroup-group.net.whatsapp.WhatsApp.shared", flat=False)
    path = "Media/34616014470@s.whatsapp.net/0/c/0c43ef63-808e-46b6-bfe8-5a2c8fce8ed2.jpg"
    sha = hashlib.sha1(("AppDomainGroup-group.net.whatsapp.WhatsApp.shared-Message/" + path).encode()).hexdigest()
    assert ref(path) == f"{sha[:2]}/{sha}"


def test_media_ref_ios7_flat_backup():
    ref = MediaRef("AppDomain-net.whatsapp.WhatsApp", flat=True, prefix="Library")
    path = "/Media/40732073334@s.whatsapp.net/3/6/36e4d5c237cad4b0eb0b4203b5b2495b.jpg"
    sha = hashlib.sha1(("AppDomain-net.whatsapp.WhatsApp-Library" + path).encode()).hexdigest()
    assert ref(path) == sha
