"""Known payees and default amount cap, computed in code from the pre-task environment.

A "known payee" is the recipient of one of the user's OWN past outgoing transactions or
scheduled transactions (the bank "first-time payee" rule: paying someone you have paid
before is lower risk). Senders of incoming transactions are deliberately excluded: anyone
can send you money, so receiving money from an IBAN says nothing about whether paying it is
authorized.
"""

from agentdojo.default_suites.v1.banking.task_suite import BankingEnvironment

# In the AgentDojo banking data, past outgoing transactions use the literal sender "me"
# while scheduled transactions use the account IBAN. Treat both as "the user".
_SELF_ALIASES = {"me"}


def normalize_iban(value: str | None) -> str:
    return "".join(str(value or "").split()).upper()


def _is_self(party: str, own_iban: str) -> bool:
    p = normalize_iban(party)
    return p in {a.upper() for a in _SELF_ALIASES} or p == normalize_iban(own_iban)


def outgoing_transactions(env: BankingEnvironment):
    own = env.bank_account.iban
    txs = list(env.bank_account.transactions) + list(env.bank_account.scheduled_transactions)
    return [t for t in txs if _is_self(t.sender, own) and not _is_self(t.recipient, own)]


def known_payees(env: BankingEnvironment) -> set[str]:
    return {normalize_iban(t.recipient) for t in outgoing_transactions(env)}


def incoming_senders(env: BankingEnvironment) -> set[str]:
    own = env.bank_account.iban
    return {
        normalize_iban(t.sender)
        for t in env.bank_account.transactions
        if _is_self(t.recipient, own) and not _is_self(t.sender, own)
    }


def max_historical_outgoing(env: BankingEnvironment) -> float:
    amounts = [t.amount for t in outgoing_transactions(env)]
    return max(amounts) if amounts else 0.0


def resolve_payee_name(name: str, env: BankingEnvironment) -> str | None:
    """Map a merchant name (e.g. "Spotify") to the IBAN of a past outgoing transaction whose
    subject mentions it. Used only by the oracle, because two AgentDojo ground truths use names
    instead of IBANs (user_task_5: "Spotify", user_task_11: "Apple")."""
    needle = name.strip().lower()
    for t in outgoing_transactions(env):
        if needle and needle in t.subject.lower():
            return normalize_iban(t.recipient)
    return None
