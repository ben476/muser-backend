import datetime
import json
import math
import os
import time
from collections import defaultdict

import requests
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, request, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room, send
from flask_sqlalchemy import SQLAlchemy
from os.path import join, dirname

# Initialise
load_dotenv(join(dirname(__file__), "..", ".env"))
app = Flask(__name__, static_folder="../frontend/dist", static_url_path="")
app.config["SECRET_KEY"] = os.environ["SECRET_KEY"] or "something secret"
app.config["SPOTIFY_CLIENT_ID"] = os.environ["SPOTIFY_CLIENT_ID"]
app.config["SPOTIFY_CLIENT_SECRET"] = os.environ["SPOTIFY_CLIENT_SECRET"]
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///db.sqlite"
app.config["SQLALCHEMY_COMMIT_ON_TEARDOWN"] = True

# Plugins
db = SQLAlchemy(app)
socketio = SocketIO(
    app, cors_allowed_origins=["http://localhost:8080", "http://localhost:3000"]
)

# DB models
class Room(db.Model):
    id = db.Column(db.Text, primary_key=True)
    images = db.Column(db.Text)
    name = db.Column(db.Text, nullable=False)
    room_type = db.Column(db.Text, nullable=False)
    messages = db.relationship("Message", backref="room", lazy=True)


class User(db.Model):
    id = db.Column(db.String(30), primary_key=True)
    display_name = db.Column(db.String(30))
    email = db.Column(db.String(320))
    profile_picture = db.Column(db.Text)
    spotify_link = db.Column(db.Text)
    href = db.Column(db.Text)

    messages = db.relationship("Message", backref="user")
    tokens = db.relationship("Token", backref="user")
    refresh_token = db.Column(db.String(256))
    access_token = db.Column(db.String(256))
    access_token_expiry = db.Column(db.DateTime)

    artist_room_1 = db.Column(db.Text)
    artist_room_2 = db.Column(db.Text)
    genre_room_1 = db.Column(db.Text)
    genre_room_2 = db.Column(db.Text)
    rooms_expiry = db.Column(db.DateTime)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, index=True, default=datetime.datetime.now)
    user_id = db.Column(db.String(30), db.ForeignKey("user.id"), nullable=False)
    room_id = db.Column(db.Text, db.ForeignKey("room.id"), nullable=False)
    # room = db.relationship("Room", backref=db.backref("messages", lazy=True))

    def marshal(self):
        return {
            "id": self.id,
            "body": self.body,
            "timestamp": self.timestamp.timestamp(),
            # "room": {
            #     "id": self.room.id,
            #     "name": self.room.name,
            #     "images": json.parse(self.room.images),
            #     "room_type": self.room.type
            # },
            "room_id": self.room_id,
            "user": {
                "id": self.user.id,
                "display_name": self.user.display_name,
                "profile_picture": self.user.profile_picture,
                "spotify_link": self.user.spotify_link,
            },
        }


class Token(db.Model):
    token = db.Column(db.String(256), primary_key=True)
    expiry = db.Column(db.DateTime)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))


# Routes

# Called after the client has recieved the spotify code
@app.route("/api/v1/spotify-callback", methods=["POST"])
def spotify_callback():
    code = request.args.get("code")
    if code is None:
        abort(400)
    print("code", code)
    token_request_payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://localhost:8080/aftersignin",
        "client_id": app.config["SPOTIFY_CLIENT_ID"],
        "client_secret": app.config["SPOTIFY_CLIENT_SECRET"],
    }
    token_request = requests.post(
        "https://accounts.spotify.com/api/token", data=token_request_payload
    )
    print("token request", token_request.text)
    token_data = token_request.json()

    user_request = requests.get(
        "https://api.spotify.com/v1/me",
        headers={"Authorization": "Bearer " + token_data["access_token"]},
    )
    print("user request", user_request.text)
    user_data = user_request.json()

    user = User.query.get(user_data["id"])

    try:
        profile_picture = user_data["images"][0]["url"]
    except:
        profile_picture = None

    if user:
        user.display_name = user_data["display_name"]
        user.email = user_data["email"]
        user.spotify_link = user_data["external_urls"]["spotify"]
        user.href = user_data["href"]
        user.profile_picture = profile_picture
        user.refresh_token = token_data["refresh_token"]
        user.access_token = token_data["access_token"]
        user.access_token_expiry = (
            datetime.timedelta(seconds=token_data["expires_in"])
            + datetime.datetime.now()
        )
    else:
        user = User(
            id=user_data["id"],
            display_name=user_data["display_name"],
            email=user_data["email"],
            profile_picture=profile_picture,
            spotify_link=user_data["external_urls"]["spotify"],
            href=user_data["href"],
            refresh_token=token_data["refresh_token"],
            access_token=token_data["access_token"],
            access_token_expiry=datetime.timedelta(seconds=token_data["expires_in"])
            + datetime.datetime.now(),
        )
        db.session.add(user)

    token = Token(
        token=code,
        expiry=datetime.datetime.now() + datetime.timedelta(weeks=1),
        user=user,
    )

    db.session.add(token)
    db.session.commit()
    return (
        jsonify(
            {
                "id": user.id,
                "display_name": user_data["display_name"],
                "email": user_data["email"],
                "spotify_link": user_data["external_urls"]["spotify"],
                "profile_picture": profile_picture,
            }
        ),
        201,
    )


@app.route("/api/v1/users/<id>")
def get_user(id):
    user = User.query.get(id)
    if not user:
        abort(400)
    return jsonify({"id": user.id})


@app.route("/api/v1/rooms")
def get_rooms():
    token = Token.query.get(request.headers.get("Authorization"))
    if token is None:
        abort(403)
    if token.expiry < datetime.datetime.now():
        abort(403)
    user = token.user

    rooms = []

    # Check if user has got today's rooms, if so get them from spotify, otherwise just get them from db
    if (not user.rooms_expiry) or user.rooms_expiry < datetime.datetime.now():
        if user.access_token_expiry < datetime.datetime.now():
            print("getting new access token")
            token_request_payload = {
                "grant_type": "refresh_token",
                "refresh_token": user.refresh_token,
                "client_id": app.config["SPOTIFY_CLIENT_ID"],
                "client_secret": app.config["SPOTIFY_CLIENT_SECRET"],
            }
            token_request = requests.post(
                "https://accounts.spotify.com/api/token", data=token_request_payload
            )
            token_data = token_request.json()
            print(token_data)
            user.access_token = token_data["access_token"]
            user.access_token_expiry = (
                datetime.timedelta(seconds=token_data["expires_in"])
                + datetime.datetime.now()
            )

        top_artists_request = requests.get(
            "https://api.spotify.com/v1/me/top/artists?limit=50",
            headers={"Authorization": "Bearer " + user.access_token},
        )
        top_artists_data = top_artists_request.json()

        if top_artists_data["total"] < 2:
            return ("Not enough data", 204)

        rooms.append(
            {
                "type": "artist",
                "name": top_artists_data["items"][0]["name"],
                "id": top_artists_data["items"][0]["id"],
                "images": top_artists_data["items"][0]["images"],
            }
        )

        user.artist_room_1 = top_artists_data["items"][0]["id"]
        user.artist_room_2 = top_artists_data["items"][1]["id"]

        rooms.append(
            {
                "type": "artist",
                "name": top_artists_data["items"][1]["name"],
                "id": top_artists_data["items"][1]["id"],
                "images": top_artists_data["items"][1]["images"],
            }
        )

        genres = defaultdict(int)
        for artist in top_artists_data["items"]:
            for genre in artist["genres"]:
                genres[genre] += 1

        genres_sorted = sorted(genres.items(), key=lambda item: item[1], reverse=True)

        rooms.append(
            {"type": "genre", "name": genres_sorted[0][0], "id": genres_sorted[0][0]}
        )
        rooms.append(
            {"type": "genre", "name": genres_sorted[1][0], "id": genres_sorted[1][0]}
        )

        user.genre_room_1 = genres_sorted[0][0]
        user.genre_room_2 = genres_sorted[1][0]

        user.rooms_expiry = datetime.datetime.combine(
            datetime.datetime.today(), datetime.time.min
        ) + datetime.timedelta(days=1)

        for room in rooms:
            if not Room.query.filter_by(id=room["id"]).first():
                if room["type"] == "artist":
                    db.session.add(
                        Room(
                            id=room["id"],
                            name=room["name"],
                            room_type="artist",
                            images=json.dumps(room["images"]),
                        )
                    )
                else:
                    db.session.add(
                        Room(id=room["name"], name=room["name"], room_type="artist")
                    )
            room_class = Room.query.filter_by(id=room["id"]).first()
            room["messages"] = [message.marshal() for message in room_class.messages]
        db.session.commit()
    else:
        for room_id in [
            user.artist_room_1,
            user.artist_room_2,
            user.genre_room_1,
            user.genre_room_2,
        ]:
            room = Room.query.filter_by(id=room_id).first()
            room_data = {"type": room.room_type, "name": room.name, "id": room.id}
            try:
                room_data["images"] = json.loads(room.images)
            except:
                print("no images")
            room_data["messages"] = [message.marshal() for message in room.messages]
            rooms.append(room_data)

    return jsonify({"rooms": rooms, "expiry": user.rooms_expiry.timestamp()})


@socketio.on("join")
def join(token_id):
    token = Token.query.get(token_id)
    if token is None:
        raise ConnectionRefusedError("authentication failed")
    if token.expiry < datetime.datetime.now():
        raise ConnectionRefusedError("authentication failed")
    user = token.user
    print(user.display_name, "has connected")
    session.token = token_id
    join_room(user.artist_room_1)
    join_room(user.artist_room_2)
    join_room(user.genre_room_1)
    join_room(user.genre_room_2)


@socketio.on("new message")
def message_created(room, body):
    user = Token.query.get(session.token).user
    if room not in (
        user.artist_room_1,
        user.artist_room_2,
        user.genre_room_1,
        user.genre_room_2,
    ):
        return "User does not have access to room", 403
    message = Message(body=body, user=user, room=Room.query.get(room))
    db.session.add(message)
    db.session.commit()
    emit(
        "new message",
        message.marshal(),
        room=room,
    )
    return "Message sent", 201


@app.errorhandler(404)
def not_found(e):
    return app.send_static_file("index.html")


if __name__ == "__main__":
    if not os.path.exists("db.sqlite"):
        db.create_all()
    socketio.run(app, port=3000, debug=True)
