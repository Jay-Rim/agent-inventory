"""Static inventory fixture; no real API calls."""
import sqlite3


def search_knowledge(query):
    return {'query': query, 'matches': []}


def generate_reply(query):
    return search_knowledge(query)
