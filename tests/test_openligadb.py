import bot


def _match(finished=True, results=None, t1="A", t2="B", date="2023-08-01T18:30:00Z"):
    return {
        "matchIsFinished": finished,
        "matchDateTimeUTC": date,
        "team1": {"teamName": t1},
        "team2": {"teamName": t2},
        "matchResults": results if results is not None else [
            {"resultTypeID": 1, "resultOrderID": 1, "pointsTeam1": 1, "pointsTeam2": 0},
            {"resultTypeID": 2, "resultOrderID": 2, "pointsTeam1": 2, "pointsTeam2": 1},
        ],
    }


def test_parse_prefers_final_result():
    parsed = bot.parse_openligadb_matches([_match()])
    assert len(parsed) == 1
    m = parsed[0]
    assert (m["home_goals"], m["away_goals"]) == (2, 1)
    assert m["team1"] == "A" and m["team2"] == "B"


def test_parse_skips_unfinished():
    assert bot.parse_openligadb_matches([_match(finished=False)]) == []


def test_parse_skips_missing_results_or_teams():
    assert bot.parse_openligadb_matches([_match(results=[])]) == []
    assert bot.parse_openligadb_matches([_match(t1="")]) == []


def test_parse_handles_none_and_empty():
    assert bot.parse_openligadb_matches(None) == []
    assert bot.parse_openligadb_matches([]) == []


def test_parse_sorts_chronologically():
    a = _match(t1="A", date="2023-09-01T00:00:00Z")
    b = _match(t1="B", date="2023-08-01T00:00:00Z")
    parsed = bot.parse_openligadb_matches([a, b])
    assert [m["team1"] for m in parsed] == ["B", "A"]


def test_final_score_falls_back_to_highest_order():
    m = _match(results=[
        {"resultOrderID": 1, "pointsTeam1": 0, "pointsTeam2": 0},
        {"resultOrderID": 2, "pointsTeam1": 3, "pointsTeam2": 2},
    ])
    parsed = bot.parse_openligadb_matches([m])
    assert (parsed[0]["home_goals"], parsed[0]["away_goals"]) == (3, 2)


def test_final_score_none_when_points_missing():
    m = _match(results=[{"resultTypeID": 2, "resultOrderID": 2, "pointsTeam1": None, "pointsTeam2": 1}])
    assert bot.parse_openligadb_matches([m]) == []
