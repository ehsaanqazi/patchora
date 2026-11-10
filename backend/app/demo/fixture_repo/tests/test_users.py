from shop.users import Profile, User, display_name


def test_display_name_uses_nickname():
    user = User(id=1, email="ada@example.com", profile=Profile(nickname="ada"))
    assert display_name(user) == "ada"


def test_display_name_falls_back_to_email():
    user = User(id=2, email="grace@example.com", profile=Profile())
    assert display_name(user) == "grace"
