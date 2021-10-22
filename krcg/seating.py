# Optimal seating computation
#
# When organising a tournament, it's important to devise an equitable seating
# which also ensures a maximum of diversity over rounds and tables.

from typing import Callable, Dict, Hashable, Iterable, List, Tuple
import collections
import concurrent.futures
import math
import numpy
import itertools
import multiprocessing
import random

# The seating rules: code, label, weight
# weights are devised so that major rules always prevail over minor rules.
# stddev rules (R3, R8) need a factor of 100 over the next one to prevail.
RULES = [
    ["R1", "predator-prey", 10 ** 10],
    ["R2", "opponent thrice", 10 ** 9],
    ["R3", "available vps", 10 ** 8],
    ["R4", "opponent twice", 10 ** 6],
    ["R5", "fifth seat", 10 ** 5],
    ["R6", "position", 10 ** 4],
    ["R7", "same seat", 10 ** 3],
    ["R8", "starting transfers", 10 ** 2],
    ["R9", "position group", 1],
]


# A helper to get relative position and position group
# keys are (table_size, relative position index)
POSITION_MEASURE = {
    (4, 0): (0, 0),  # prey, neighbour
    (4, 1): (4, 1),  # cross-table, non-neighbour
    (4, 2): (3, 0),  # predator, neighbour
    (5, 0): (0, 0),  # prey, neighbour
    (5, 1): (1, 1),  # grand-prey, non-neighbour
    (5, 2): (2, 1),  # grand-predator, non-neighbour
    (5, 3): (3, 0),  # predator, neighbour
}


class Round(list):
    """A list of list representing the tables of a round"""

    @classmethod
    def from_players(cls, players: Iterable[Hashable]):
        """Build a round structure from a simple list of players"""
        length = len(players)
        if length in {6, 7, 11}:
            raise ValueError(
                f"A staggered round structure is required for {length} players"
            )
        fours = 5 - (length % 5 or 5)
        fives = (length - 4 * fours) // 5
        return cls(
            itertools.chain(
                (players[i : i + 5] for i in range(0, fives * 5, 5)),
                (
                    players[i : i + 4]
                    for i in range(fives * 5, fives * 5 + fours * 4, 4)
                ),
            )
        )

    @classmethod
    def copy(cls, round_):
        """Copy another Round"""
        return cls(t[:] for t in round_.iter_tables())

    def shuffle(self):
        players = list(self.iter_players())
        random.shuffle(players)
        for i in range(self.players_count()):
            self[i] = players[i]

    def iter_table_players(self) -> Iterable[Tuple[int, int, int, Hashable]]:
        """Full information players iteration

        Yields: table number, position, table size, player number
        """
        for table_number, players in enumerate(self, 1):
            table_size = len(players)
            for position, player in enumerate(players, 1):
                yield table_number, position, table_size, player

    def iter_tables(self):
        """Iterate on the tables, not the players (default)"""
        yield from super().__iter__()

    def iter_players(self):
        """Convenience method for symmetry with `iter_tables`"""
        for table in super().__iter__():
            for player in table:
                yield player

    def tables_count(self):
        """Get the number of tables (default len gives the number of players)"""
        return super().__len__()

    def players_count(self):
        """Convenience function for symmetry with `tables_count`"""
        return sum(len(table) for table in self.iter_tables())

    def __global_index_to_tuple(self, index: int):
        """Private method to get the underlying index out of a naive global index"""
        table_index = 0
        for table in self.iter_tables():
            if index >= len(table):
                index -= len(table)
                table_index += 1
                continue
            return table_index, index
        else:
            raise IndexError("Out of bounds")

    def __getitem__(self, index: int):
        """Access items transparently using a global index"""
        i, j = self.__global_index_to_tuple(index)
        return super().__getitem__(i)[j]

    def __setitem__(self, index, value: Hashable):
        """Access items transparently using a global index"""
        i, j = self.__global_index_to_tuple(index)
        super().__getitem__(i)[j] = value

    # NOTE: Do not mess with default iteration to avoid problems with multiprocessing


Measure = collections.namedtuple("Measure", ["position", "opponents"])
Measure.__add__ = lambda lhs, rhs: Measure(*(a + b for a, b in zip(lhs, rhs)))
Measure.__radd__ = lambda rhs, lhs: rhs if lhs == 0 else rhs.__add__(lhs)


def measure(max_player_number: int, round_: Round) -> Measure:
    """Measure a round (list of tables), returns two matrices:
    position (players_count x 8):
        for each player:
            played, vps, transfers (integer),
            seat1, seat2, seat3, seat4, seat5 (1 for the seat they occupy)
    opponents (players_count x players_count x 8):
        for each pair of players, booleans indicating if they were:
            opponent, prey, grand-prey, grand-predator, predator,
            cross-table, neighbour, non-neighbour

    Simply adding each round measure gives the total measure.
    This allows to re-compute a single round measure when a single round is changed.
    max_player_number must be the max over all rounds so that we can add round matrices.
    """
    position = numpy.zeros((max_player_number, 8), int)
    opponents = numpy.zeros((max_player_number, max_player_number, 8), int)
    for table in round_.iter_tables():
        table_size = len(table)
        for seat, player in enumerate(table):
            position[player - 1][0] = 1
            position[player - 1][1] = table_size
            transfers = min(seat + 1, 4)
            position[player - 1][2] = transfers
            position[player - 1][3 + seat] = 1
            for relation, opponent in enumerate(
                itertools.chain(table[seat + 1 :], table[:seat])
            ):
                relation, group = POSITION_MEASURE.get((table_size, relation))
                opponents[player - 1][opponent - 1][0] = 1
                opponents[player - 1][opponent - 1][1 + relation] = 1
                opponents[player - 1][opponent - 1][6 + group] = 1
    return Measure(position, opponents)


# Violations objects listed by Score
PlayerViolation = collections.namedtuple("PlayerViolation", ["player"])
PairViolation = collections.namedtuple("PairViolation", ["player_1", "player_2"])
PositionViolation = collections.namedtuple(
    "PositionViolation", ["player_1", "player_2", "position"]
)
SeatViolation = collections.namedtuple("SeatViolation", ["player", "seat"])
Deviation = collections.namedtuple("Deviation", ["player", "value"])


class Score:
    """A detailed scoring of a seating measure.

    Original criteria:
    https://groups.google.com/g/rec.games.trading-cards.jyhad/c/4YivYLDVYQc/m/CCH-ZBU5UiUJ

    R1 No pair of players repeat their predator-prey relationship. This is mandatory.
    R2 No pair of players share a table through all three rounds, when possible.
    R3 Available VPs are equitably distributed.
    R4 No pair of players share a table more often than necessary.
    R5 A player doesn't sit in the fifth seat more than once.
    R6 No pair of players repeat the same relative position[*], when possible.
    R7 A player doesn't play in the same seat position, if possible.
    R8 Starting transfers are equitably distributed.
    R9 No pair of players repeat the same relative position group[^], when possible.

    [*] "relative position" relationship values:
    1) prey
    2) predator
    3) grand-prey at a 5
    4) grand-predator at a 5
    5) cross-table at a 4-player
    Note that repeating 1 and repeating 2 is already handled (prohibited) by R1.

    [^] "relative position group" values:
    1) Adjacent (prey or predator)
    2) Not adjacent

    The matching attributes of the instance provide a list of violations for each rule,
    except for rules R3 and R8, simply indicating the standard deviation of the value.
    For those rules, player by player violations (too far away from mean) are listed
    in the `vps` and `transfers` attributes and the mean values in `mean_vps` and
    `mean_tranfers`.
    """

    __slots__ = dict(
        itertools.chain(
            ((R[0], R[1]) for R in RULES),
            (
                ("mean_vps", "float: Mean value of accessible VPs"),
                ("mean_transfers", "float: Mean value of initial transfers"),
                ("vps", "list: [player, vp] far from mean"),
                ("transfers", "list: [player, transfers] far from mean"),
                ("rules", "list: Numeric values for the 9 rules"),
                ("total", "float: Total weighted score use for comparison"),
            ),
        )
    )

    def __init__(self, rounds: List[Round], max_player_number: int = None):
        max_player_number = max_player_number or _max_player_number(rounds)
        self.score_measure(
            sum(measure(max_player_number, r) for r in rounds), len(rounds)
        )

    def score_measure(self, measure: Measure, rounds_count: int) -> None:
        # transfers, starting vps: compute standard deviation
        vps = measure.position[:, 1] / measure.position[:, 0]
        transfers = measure.position[:, 2] / measure.position[:, 0]
        self.R3 = numpy.std(vps)
        self.R8 = numpy.std(transfers)
        # record details of anomalies for output
        self.mean_vps = numpy.mean(vps)
        self.mean_transfers = numpy.mean(transfers)
        self.vps = [
            Deviation(i + 1, vps[i])
            for i in numpy.flatnonzero(abs(self.mean_vps - vps) > 1 / rounds_count)
        ]
        self.transfers = [
            Deviation(i + 1, transfers[i])
            for i in numpy.flatnonzero(
                abs(self.mean_transfers - transfers) > 1 / rounds_count
            )
        ]
        # same seat twice (or more)
        self.R7 = [
            SeatViolation(*v) for v in (numpy.argwhere(measure.position[:, 3:] > 1) + 1)
        ]
        # fifth seat twice (or more)
        self.R5 = [PlayerViolation(a) for a, s in self.R7 if s == 5]
        # opponent twice (or more)
        # note the initial list has every pair twice (symmetry), hence the `if a < b`
        self.R4 = [
            PairViolation(a, b)
            for a, b in (numpy.argwhere(measure.opponents[:, :, 0] > 1) + 1)
            if a < b
        ]
        # opponent thrice (or more)
        self.R2 = [
            PairViolation(a, b)
            for a, b in (numpy.argwhere(measure.opponents[:, :, 0] > 2) + 1)
            if a < b
        ]
        # same position twice (or more)
        self.R6 = [
            PositionViolation(a, b, p)
            for a, b, p in (numpy.argwhere(measure.opponents[:, :, 1:6] > 1) + 1)
            if a < b
        ]
        # predator-prey twice (or more)
        self.R1 = [PairViolation(a, b) for a, b, p in self.R6 if p in [1, 4]]
        # same position group twice (or more)
        self.R9 = [
            PositionViolation(a, b, g)
            for a, b, g in (numpy.argwhere(measure.opponents[:, :, 6:] > 1) + 1)
            if a < b
        ]
        # individual score for each rule
        self.rules = [
            getattr(self, R[0]) if R[0] in ["R3", "R8"] else len(getattr(self, R[0]))
            for R in RULES
        ]
        self.total = sum(x * m for x, m in zip(self.rules, [R[2] for R in RULES]))

    @staticmethod
    def fast_total(measure: Measure) -> float:
        """Get just a total score of a seating measure (all rounds).

        This is used to speed up computations when searching for an optimum.
        """
        rules = [
            len(numpy.argwhere(measure.opponents[:, :, 1] > 1)),
            len(numpy.argwhere(measure.opponents[:, :, 0] > 2)) // 2,
            numpy.std(measure.position[:, 1] / measure.position[:, 0]),
            len(numpy.argwhere(measure.opponents[:, :, 0] > 1)) // 2,
            len(numpy.argwhere(measure.position[:, 6] > 1)),
            len(numpy.argwhere(measure.opponents[:, :, 1:6] > 1)) // 2,
            len(numpy.argwhere(measure.position[:, 3:] > 1)),
            numpy.std(measure.position[:, 2] / measure.position[:, 0]),
            len(numpy.argwhere(measure.opponents[:, :, 6:] > 1)) // 2,
        ]
        return sum(x * m for x, m in zip(rules, [R[2] for R in RULES]))


def get_rounds(players_count: int, rounds_count: int) -> List[Round]:
    """Return the base rounds for given parameters

    This gets complicated only if you got 6, 7 or 11 players, otherwise it's simply
    the list of all players for each round.

    >>> rounds(8, 3)
    [[[1, 2, 3, 4], [5, 6, 7, 8]],
     [[1, 2, 3, 4], [5, 6, 7, 8]],
     [[1, 2, 3, 4], [5, 6, 7, 8]]]
    """
    if players_count < 4:
        raise RuntimeError("At least 4 players required")

    if players_count not in [6, 7, 11]:
        base = list(range(1, players_count + 1))
        return [Round.from_players(base[:]) for _ in range(rounds_count)]

    if rounds_count < 2:
        raise RuntimeError("At least 2 rounds by player are required")

    # number of players you can remove to be able to play
    possible_outs = []
    for i in [4, 5, 4 + 4, 4 + 5, 5 + 5]:
        if players_count <= i:
            break
        possible_outs.insert(0, players_count - i)
    # check how many additional rounds we need for everybody to play the required rounds
    # the way we do it is count the minimum number of times each player has to sit out:
    # players:                1 2 3 4 5 6 7
    # sit out once in round   1 1 2 2 3 3
    # we have to exclude 2 players per round minimum, but if we have 3 rounds
    # (counting the additional round), it is ok. If we have more we need more "cycles"
    # players:                1 2 3 4 5 6 7
    # sit out once in round   1 1 2 2 3 3 4
    # sit out twice in round  4
    # in the next step we adapt the number of sit outs to match the number of players
    additional_rounds = 1
    while (
        possible_outs[0] * (rounds_count + additional_rounds)
        > players_count * additional_rounds
    ):
        additional_rounds += 1
    rounds_count = rounds_count + additional_rounds
    excludes = players_count * additional_rounds
    out = []
    # compute how many players you exclude for each round:
    # for each round you must exclude a "possible outs" number,
    # the total must match exactly  #players * #additional_rounds:
    # this is a change-making problem, we use the glutton algorithm
    # but need to be careful about the case when the least number of outs
    # is not 1, but 2 - luckily in that case, 3 is also authorized.
    while excludes:
        i = 0
        while (rounds_count - len(out)) * possible_outs[i] < excludes:
            i += 1
        # the first two possible_outs are always consecutive,
        # checking the last pair is sufficient
        while i > 0 and 0 < excludes - possible_outs[i] < possible_outs[0]:
            i -= 1
        out.append(possible_outs[i])
        excludes -= possible_outs[i]
    exclusions = sum([[i] * out[i] for i in range(rounds_count)], [])
    return [
        Round.from_players(
            [
                p
                for p in range(1, players_count + 1)
                if not any(
                    exclusions[p - 1 + players_count * c] == r
                    for c in range(additional_rounds)
                )
            ]
        )
        for r in range(rounds_count)
    ]


def _max_player_number(rounds: List[Round]) -> int:
    """Internal function used to get the matrix dimension required for measures."""
    return max(
        p for p in itertools.chain.from_iterable(r.iter_players() for r in rounds)
    )


def optimise(
    rounds: List[Round],
    iterations: int,
    fixed: int = None,
    callback: Callable = None,
) -> Tuple[List[Round], Score]:
    """Given a list of players for each round, compute an optimal seating.

    - callback is called every 100th of the way with the following keyword arguments:
        * step
        * temperature
        * score
        * trials (since last callback call)
        * accepts (since last callback call)
        * improves (since last callback call)
    - fixed is the number of rounds that are left untouched by the optimisation
      (default is all except the last one)

    Use a simulated annealing algorithm:
        - exponential cooldown strategy
        - given the problem shape, reset the state to the best known one regularily

    Using a list of tables per round as entry allows the function to be used for
    corner cases like:
        - 6, 7 and 11 players seatings
        - change of players in the middle of a tournament

    Iterations:
        empyrism shows that the best results are achieved calling
        this 4x with around 20k iterations each for a full 3R+F round structure

        For successive calls to build each round as they come,
        players going in and out of them, a simple 20k iteration is sufficient.
    """
    random.seed()
    # annealing parameters have been chosen experimentally
    temperature_min = 0.001
    temperature_max = RULES[0][2]
    temperature_factor = -math.log(temperature_max / temperature_min)
    max_player_number = _max_player_number(rounds)
    # initial state
    if fixed is None:
        fixed = len(rounds) - 1
    temperature = temperature_max
    measures = [measure(max_player_number, r) for r in rounds]
    best_score = previous_score = score = Score.fast_total(sum(measures))
    best_state = [Round.copy(r) for r in rounds]
    for round_ in rounds[fixed:]:
        round_.shuffle()
    trials, accepts, improves = 0, 0, 0

    # Exploration
    for step in range(iterations):
        temperature = temperature_max * math.exp(temperature_factor * step / iterations)
        round_index = random.randrange(fixed, len(rounds))
        round_ = rounds[round_index]
        # note all rounds might not have the same length
        length = round_.players_count()
        i = random.randrange(length)
        j = random.randrange(length)
        round_[i], round_[j] = round_[j], round_[i]
        # only recompute the changed round, other rounds have not varied
        previous_measure = measures[round_index]
        measures[round_index] = measure(max_player_number, round_)
        score = Score.fast_total(sum(measures))
        score_diff = score - previous_score
        trials += 1
        # accept or reject the move depending on its score and temperature
        # the higher temperature, the higher the chance to accept a non-improving move
        if score_diff > 0 and math.exp(-score_diff / temperature) < random.random():
            round_[i], round_[j] = round_[j], round_[i]
            score = previous_score
            measures[round_index] = previous_measure
        else:
            accepts += 1
            previous_score = score
            if score_diff < 0.0:
                improves += 1
            if score < best_score:
                best_state = [Round.copy(r) for r in rounds]
                best_score = score
        # every 100th of the way, call the callback
        # and reset the state to the best known state
        if step and not step % (iterations // 100 or 1):
            if callback:
                callback(
                    step=step,
                    temperature=temperature,
                    score=score,
                    trials=trials,
                    accepts=accepts,
                    improves=improves,
                )
            trials, accepts, improves = 0, 0, 0
            rounds = [Round.copy(r) for r in best_state]
            measures = [measure(max_player_number, r) for r in rounds]
            previous_score = best_score

    return best_state, Score(best_state, max_player_number=max_player_number)


def archon_seating(players_count: int, rounds_per_player: int):
    """Convenience function to compute a full multiround seating"""
    rounds = get_rounds(players_count, rounds_per_player)
    try:
        cpus = multiprocessing.cpu_count()
    except NotImplementedError:
        cpus = 1
    results = []
    with concurrent.futures.ProcessPoolExecutor() as executor:
        for _ in range(cpus):
            results.append(executor.submit(optimise, rounds, 80000, 1))
        results = [r.result() for r in results]
        return min(results, key=lambda x: x[1].total)
