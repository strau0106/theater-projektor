import Database from "better-sqlite3";
import { mkdirSync } from "node:fs";
import path from "node:path";

const g = globalThis as unknown as { __welleDb?: Database.Database };

export function db(): Database.Database {
  if (!g.__welleDb) {
    const dir = path.resolve(/*turbopackIgnore: true*/ process.env.DATA_DIR || "./data");
    mkdirSync(dir, { recursive: true });
    const d = new Database(path.join(dir, "vote.db"));
    d.pragma("journal_mode = WAL");
    d.exec(`
      CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        closed_at TEXT
      );
      CREATE TABLE IF NOT EXISTS votes (
        poll_id INTEGER NOT NULL REFERENCES polls(id),
        voter_id TEXT NOT NULL,
        choice TEXT NOT NULL CHECK (choice IN ('ja','nein')),
        created_at TEXT NOT NULL,
        UNIQUE (poll_id, voter_id)
      );
    `);
    g.__welleDb = d;
  }
  return g.__welleDb;
}
