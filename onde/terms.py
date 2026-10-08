"""Conditions d'utilisation d'Onde : acceptées à l'installation (installateur Windows), à la création
d'un profil (lien d'invitation, appli) et une fois par profil existant (dans l'appli).

Ce texte rappelle les règles et met la responsabilité sur la personne qui les enfreint ; il ne rend pas
légal ce qui ne l'est pas (voir README : usage privé, petit cercle, pas de partage)."""
from __future__ import annotations

import html
import time

from . import db

VERSION = "2026-10"

TITLE = "Conditions d'utilisation d'Onde"

SECTIONS: list[tuple[str, list[str]]] = [
    ("1. À quoi sert Onde", [
        "Onde est un serveur de musique personnel : il sert à écouter, chez soi et sur ses appareils, "
        "une bibliothèque musicale privée gérée par son administrateur.",
        "L'accès est réservé aux personnes invitées personnellement par l'administrateur.",
    ]),
    ("2. Usage strictement privé", [
        "Onde s'utilise uniquement à titre privé et personnel, dans le cercle restreint de l'administrateur.",
        "Aucun usage commercial : pas de vente, pas d'abonnement, pas de publicité, pas de diffusion publique "
        "(bar, magasin, événement, radio, stream…).",
    ]),
    ("3. Ce qui est interdit", [
        "Partager, revendre, copier ou redistribuer à qui que ce soit la musique, les fichiers, les pochettes "
        "ou les paroles disponibles dans Onde.",
        "Donner à quelqu'un d'autre son pseudo, son mot de passe, un lien d'invitation ou un lien de partage, "
        "ou publier une adresse d'accès à Onde (réseaux sociaux, forums, groupes…).",
        "Rendre Onde accessible au public, contourner les droits de son profil ou tenter d'accéder aux "
        "données des autres.",
        "Importer ou demander des contenus que l'on n'a pas le droit de détenir.",
    ]),
    ("4. Responsabilité", [
        "Chaque utilisateur est seul responsable de ce qu'il importe, demande, écoute et fait des contenus "
        "auxquels il a accès, ainsi que du respect des droits d'auteur et des lois de son pays.",
        "L'utilisateur qui ne respecte pas ces règles en assume seul toutes les conséquences, notamment "
        "juridiques et financières, et garantit l'administrateur contre toute réclamation qui en découlerait.",
        "Onde est fourni tel quel, sans garantie.",
    ]),
    ("5. Contrôle et sanctions", [
        "Les connexions et les actions (imports, demandes, partages) sont enregistrées dans un journal "
        "consultable par l'administrateur.",
        "En cas de non-respect, l'administrateur peut suspendre ou supprimer l'accès immédiatement et sans "
        "préavis, et se réserve le droit d'engager des poursuites.",
    ]),
    ("6. Acceptation", [
        "Installer Onde, créer un profil ou l'utiliser vaut acceptation complète de ces conditions. "
        "Si tu ne les acceptes pas, n'installe pas Onde et ne l'utilise pas.",
    ]),
]


def text() -> str:
    """Version texte (installateur Windows : CONDITIONS.txt)."""
    out = [TITLE, f"Version {VERSION}", ""]
    for head, paras in SECTIONS:
        out += [head, *[f"- {p}" for p in paras], ""]
    return "\r\n".join(out)


def html_body() -> str:
    return "".join(f"<h3>{html.escape(h)}</h3><ul>{''.join(f'<li>{html.escape(p)}</li>' for p in ps)}</ul>"
                   for h, ps in SECTIONS)


def accepted(user_id: int) -> bool:
    row = db.q1("SELECT terms_version FROM users WHERE id=?", user_id)
    return bool(row and row["terms_version"] == VERSION)


def accept(user_id: int) -> None:
    db.x("UPDATE users SET terms_version=?, terms_at=? WHERE id=?", VERSION, time.time(), user_id)
