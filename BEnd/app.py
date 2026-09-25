import difflib
import os
import re
import time
import requests
import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

DB_CONFIG = dict(
    host="localhost",
    dbname="tamil",
    user="postgres",
    password="tamil123",
)

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id SERIAL PRIMARY KEY,
            username VARCHAR(80) UNIQUE NOT NULL,
            email VARCHAR(120) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            submission_id SERIAL PRIMARY KEY,
            user_id INT REFERENCES users(user_id),
            input_text TEXT,
            clean_text TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS corrections (
            correction_id SERIAL PRIMARY KEY,
            submission_id INT REFERENCES submissions(submission_id),
            error_type VARCHAR(30),
            corrected_text TEXT,
            explanation TEXT
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            score_id SERIAL PRIMARY KEY,
            submission_id INT REFERENCES submissions(submission_id),
            spelling_score NUMERIC(5, 2),
            grammar_score NUMERIC(5, 2),
            sentence_score NUMERIC(5, 2),
            overall_score NUMERIC(5, 2)
        );
        """
    )

    conn.commit()
    cur.close()
    conn.close()

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("chat"))
    return render_template("index.html", active_tab="login")

