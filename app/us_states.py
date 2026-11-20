"""The ways a US state is written in parentheses after a school's name: `Miami (OH)`,
`Miami (Ohio)`, `IMG (Fla.)`. TheSportsDB spells the postal code and guides spell any of the three,
so a team name carrying one reads as all of them (sports_match._load_teams, dev/changelog/1662).

(postal code, name, the Associated Press abbreviation or '' where AP spells the name out).
"""
import re

STATES = (
    ('AL', 'Alabama', 'Ala.'), ('AK', 'Alaska', ''), ('AZ', 'Arizona', 'Ariz.'), ('AR', 'Arkansas', 'Ark.'),
    ('CA', 'California', 'Calif.'), ('CO', 'Colorado', 'Colo.'), ('CT', 'Connecticut', 'Conn.'),
    ('DE', 'Delaware', 'Del.'), ('DC', 'District of Columbia', 'D.C.'), ('FL', 'Florida', 'Fla.'),
    ('GA', 'Georgia', 'Ga.'), ('HI', 'Hawaii', ''), ('ID', 'Idaho', ''), ('IL', 'Illinois', 'Ill.'),
    ('IN', 'Indiana', 'Ind.'), ('IA', 'Iowa', ''), ('KS', 'Kansas', 'Kan.'), ('KY', 'Kentucky', 'Ky.'),
    ('LA', 'Louisiana', 'La.'), ('ME', 'Maine', ''), ('MD', 'Maryland', 'Md.'), ('MA', 'Massachusetts', 'Mass.'),
    ('MI', 'Michigan', 'Mich.'), ('MN', 'Minnesota', 'Minn.'), ('MS', 'Mississippi', 'Miss.'),
    ('MO', 'Missouri', 'Mo.'), ('MT', 'Montana', 'Mont.'), ('NE', 'Nebraska', 'Neb.'), ('NV', 'Nevada', 'Nev.'),
    ('NH', 'New Hampshire', 'N.H.'), ('NJ', 'New Jersey', 'N.J.'), ('NM', 'New Mexico', 'N.M.'),
    ('NY', 'New York', 'N.Y.'), ('NC', 'North Carolina', 'N.C.'), ('ND', 'North Dakota', 'N.D.'),
    ('OH', 'Ohio', ''), ('OK', 'Oklahoma', 'Okla.'), ('OR', 'Oregon', 'Ore.'), ('PA', 'Pennsylvania', 'Pa.'),
    ('RI', 'Rhode Island', 'R.I.'), ('SC', 'South Carolina', 'S.C.'), ('SD', 'South Dakota', 'S.D.'),
    ('TN', 'Tennessee', 'Tenn.'), ('TX', 'Texas', ''), ('UT', 'Utah', ''), ('VT', 'Vermont', 'Vt.'),
    ('VA', 'Virginia', 'Va.'), ('WA', 'Washington', 'Wash.'), ('WV', 'West Virginia', 'W.Va.'),
    ('WI', 'Wisconsin', 'Wis.'), ('WY', 'Wyoming', 'Wyo.'),
)

_PAREN = re.compile(r'\(([^()]+)\)')
#: {a spelling, casefolded with its periods off: every spelling of that state}. Built once, never changed.
_BY_SPELLING = {spelling.casefold().replace('.', ''): tuple(s for s in row if s)
                for row in STATES for spelling in row if spelling}


def state_spellings(name: str) -> list:
    """`name` with its parenthesized state written every way, itself first: `Miami (OH)` gives
    `Miami (OH)`, `Miami (Ohio)`. A name with no state in parentheses comes back alone. Only what
    stands in parentheses is read, so `Ohio State` and `Washington` are left as they are."""
    found = _PAREN.search(name or '')
    row = _BY_SPELLING.get(found.group(1).strip().casefold().replace('.', '')) if found else None
    if row is None:
        return [name]
    out = [name]
    for spelling in row:
        other = f'{name[:found.start()]}({spelling}){name[found.end():]}'
        if other not in out:
            out.append(other)
    return out
