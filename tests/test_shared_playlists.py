"""Playlists partagées entre profils : le propriétaire invite, les invités ajoutent/retirent, seul lui modifie."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from onde import api_models as M
from onde import config, db, main, users


@pytest.fixture
def world(fresh_db):
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'A', 'Alice')")
    for i in (1, 2, 3):
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, duration, search) VALUES(?,?,?,?,1,100,?)", i, f"/m/{i}.mp3", f"T{i}", "Alice", f"t{i}")
    users.update(1, password="admin-pass")
    pote = users.create("Pote", "pote-pass", "member")
    ecoute = users.create("Ecoute", "ecoute-pass", "listener")
    clients = {}
    for key, name, pw in (("admin", config.ADMIN_NAME, "admin-pass"), ("Pote", "Pote", "pote-pass"), ("Ecoute", "Ecoute", "ecoute-pass")):
        c = TestClient(main.app)
        r = c.post("/api/auth/login", json={"name": name, "password": pw})
        assert r.status_code == 200, r.text
        clients[key] = c
    return clients, pote["id"], ecoute["id"]


def test_share_add_remove_and_permissions(world):
    c, pote, ecoute = world
    pid = c["admin"].post("/api/playlists", json={"name": "Soirée"}).json()["id"]
    assert c["Pote"].get(f"/api/playlists/{pid}").status_code == 404  # pas encore partagée
    r = c["admin"].put(f"/api/playlists/{pid}/members", json={"userIds": [pote]})
    assert r.status_code == 200 and [m["name"] for m in r.json()["members"]] == ["Pote"]
    # l'invité la voit, ajoute un titre, voit « ajouté par »
    assert any(p["id"] == pid and not p["mine"] and p["ownerName"] for p in c["Pote"].get("/api/playlists").json())
    assert c["Pote"].post(f"/api/playlists/{pid}/tracks", json={"trackIds": [2]}).status_code == 200
    tracks = c["admin"].get(f"/api/playlists/{pid}").json()["tracks"]
    assert [t["id"] for t in tracks] == [2] and tracks[0]["addedBy"] == "Pote"
    # l'invité ne peut ni renommer, ni supprimer, ni repartager, ni changer l'image
    assert c["Pote"].patch(f"/api/playlists/{pid}", json={"name": "X"}).status_code == 403
    assert c["Pote"].delete(f"/api/playlists/{pid}").status_code == 403
    assert c["Pote"].put(f"/api/playlists/{pid}/members", json={"userIds": []}).status_code == 403
    assert c["Pote"].delete(f"/api/playlists/{pid}/cover").status_code == 403
    # un profil non invité ne la voit pas
    assert c["Ecoute"].get(f"/api/playlists/{pid}").status_code == 404
    # le propriétaire retire l'accès
    c["admin"].put(f"/api/playlists/{pid}/members", json={"userIds": []})
    assert c["Pote"].get(f"/api/playlists/{pid}").status_code == 404


def test_owner_cannot_be_added_and_unknown_ids_ignored(world):
    c, pote, _ = world
    pid = c["admin"].post("/api/playlists", json={"name": "P"}).json()["id"]
    r = c["admin"].put(f"/api/playlists/{pid}/members", json={"userIds": [1, 999, pote]})
    assert [m["id"] for m in r.json()["members"]] == [pote]
    assert c["admin"].put(f"/api/playlists/{pid}/members", json={"userIds": "x"}).status_code == 400


def test_people_excludes_me(world):
    c, pote, ecoute = world
    assert {p["name"] for p in c["Pote"].get("/api/people").json()} == {"Ecoute", config.ADMIN_NAME}
    assert all(p["id"] != pote for p in c["Pote"].get("/api/people").json())


def test_delete_owner_playlist_cascades_and_deleting_member_cleans_up(world):
    c, pote, _ = world
    pid = c["admin"].post("/api/playlists", json={"name": "P"}).json()["id"]
    c["admin"].put(f"/api/playlists/{pid}/members", json={"userIds": [pote]})
    users.delete(pote)
    assert db.q1("SELECT COUNT(*) n FROM playlist_members")["n"] == 0
    c["admin"].put(f"/api/playlists/{pid}/members", json={"userIds": []})
    assert c["admin"].delete(f"/api/playlists/{pid}").status_code == 200
