"""Human-readable names for game enum ids (2026 Season Pack values)."""

TRACKS = {
    -1: "Unknown", 0: "Melbourne", 2: "Shanghai", 3: "Bahrain", 4: "Barcelona",
    5: "Monaco", 6: "Montreal", 7: "Silverstone", 9: "Hungaroring", 10: "Spa",
    11: "Monza", 12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi", 15: "Austin",
    16: "Interlagos", 17: "Red Bull Ring", 19: "Mexico City", 20: "Baku",
    26: "Zandvoort", 27: "Imola", 29: "Jeddah", 30: "Miami", 31: "Las Vegas",
    32: "Qatar", 39: "Silverstone (R)", 40: "Red Bull Ring (R)",
    41: "Zandvoort (R)", 42: "Madrid",
}

SESSION_TYPES = {
    0: "Unknown", 1: "P1", 2: "P2", 3: "P3", 4: "Short Practice",
    5: "Q1", 6: "Q2", 7: "Q3", 8: "Short Quali", 9: "One-Shot Quali",
    10: "Sprint SO1", 11: "Sprint SO2", 12: "Sprint SO3",
    13: "Short Sprint SO", 14: "One-Shot Sprint SO",
    15: "Race", 16: "Race 2", 17: "Race 3", 18: "Time Trial",
}

# Constructors (F1 25 grid, 2024 retro grid, 2026 Season Pack grid)
TEAMS = {
    0: "Mercedes", 1: "Ferrari", 2: "Red Bull", 3: "Williams",
    4: "Aston Martin", 5: "Alpine", 6: "Racing Bulls", 7: "Haas",
    8: "McLaren", 9: "Sauber",
    41: "F1 Generic", 104: "Custom Team", 129: "Konnersport",
    142: "APXGP '24", 154: "APXGP '25", 155: "Konnersport '24",
    185: "Mercedes '24", 186: "Ferrari '24", 187: "Red Bull '24",
    188: "Williams '24", 189: "Aston Martin '24", 190: "Alpine '24",
    191: "Racing Bulls '24", 192: "Haas '24", 193: "McLaren '24",
    194: "Sauber '24",
    476: "Mercedes '26", 477: "Ferrari '26", 478: "Red Bull '26",
    479: "Williams '26", 480: "Aston Martin '26", 481: "Alpine '26",
    482: "Racing Bulls '26", 483: "Haas '26", 484: "McLaren '26",
    485: "Audi '26", 486: "Cadillac '26",
    # The 2025 UDP layout carries teamId as a single byte, so the game
    # truncates the '26 grid ids (476-486) to their low byte (220-230).
    220: "Mercedes '26", 221: "Ferrari '26", 222: "Red Bull '26",
    223: "Williams '26", 224: "Aston Martin '26", 225: "Alpine '26",
    226: "Racing Bulls '26", 227: "Haas '26", 228: "McLaren '26",
    229: "Audi '26", 230: "Cadillac '26",
}

def track_name(track_id):
    return TRACKS.get(track_id, "Track %d" % track_id)


def session_type_name(st):
    return SESSION_TYPES.get(st, "Session %d" % st)


def team_name(team_id):
    if team_id is None:
        return None
    return TEAMS.get(team_id, "Team %d" % team_id)
