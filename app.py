from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from datetime import datetime
from functools import wraps
import requests
import boto3
import time
import os

load_dotenv() 
app = Flask(__name__)

s3 = boto3.client('s3')

# Retrieve environment variables
SOURCE_BUCKET = os.getenv('SOURCE_BUCKET')
TARGET_BUCKET = os.getenv('TARGET_BUCKET')
API_GATEWAY_URL = os.getenv('API_GATEWAY_URL')
MYSQL_USERNAME = os.getenv('MYSQL_USERNAME')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE')

# Configuration
app.config["SQLALCHEMY_DATABASE_URI"] = f"mysql://{MYSQL_USERNAME}:{MYSQL_PASSWORD}@{MYSQL_HOST}/{MYSQL_DATABASE}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.secret_key = 'your_secret_key'  # Change this to a random secret key
db = SQLAlchemy(app)

# User Model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(128), nullable=False)
    registration_date = db.Column(db.DateTime, default=datetime.utcnow)

# Create the database tables if they don't exist
with app.app_context():
    db.create_all()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('You need to log in first.', 'danger')
            return redirect(url_for('login'))  # Redirect to login if not authenticated
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('resize_image'))  # Redirect to resize_image if logged in
    return render_template('index.html')

@app.route('/register', methods=['POST'])
def register():
    username = request.form['uname']
    email = request.form['email']
    password = request.form['psw']

    hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
    
    new_user = User(username=username, email=email, password=hashed_password)
    db.session.add(new_user)
    db.session.commit()

    return jsonify({'message': 'User registered successfully! Please log in.'})

@app.route('/check-availability', methods=['POST'])
def check_availability():
    data = request.get_json()
    username = data.get('uname', '')
    email = data.get('email', '')

    username_exists = User.query.filter_by(username=username).first() is not None
    email_exists = User.query.filter_by(email=email).first() is not None

    return jsonify({'usernameExists': username_exists, 'emailExists': email_exists})

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Check if the user is already logged in
    if 'user_id' in session:
        return redirect(url_for('resize_image'))  # Redirect to resize_image if logged in

    if request.method == 'POST':
        username = request.form['uname']
        password = request.form['psw']
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            session['username'] = user.username
            flash('Login successful!', 'success')
            return redirect(url_for('resize_image'))  # Redirect to resize_image after successful login
        else:
            flash('Login failed! Check your username and/or password.', 'danger')

    return render_template('login.html')  # Render login page for GET requests

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))

@app.route('/resize-image')
@login_required
def resize_image():
    return render_template('resize-image.html')

@app.route('/upload', methods=['POST'])
@login_required
def upload_image():
    file = request.files['file']
    width = int(request.form['width'])
    height = int(request.form['height'])

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    extension = file.filename.rsplit('.', 1)[-1]
    source_key = f"{timestamp}.{extension}"

    # Upload file to S3
    try:
        s3.upload_fileobj(file, SOURCE_BUCKET, source_key)
        print("Image uploaded successfully.")
        time.sleep(5) # The time where lambda function complete it's task
    except Exception as e:
        return jsonify({'message': 'File upload failed.', 'error': str(e)}), 500

    # Call API Gateway to trigger Lambda with resize instructions
    response = requests.post(API_GATEWAY_URL, json={
        'source_key': source_key,
        'width': width,
        'height': height,
        'source_bucket': SOURCE_BUCKET,
        'target_bucket': TARGET_BUCKET
    })

    if response.status_code != 200:
        return jsonify({'message': 'Image resizing failed.'}), 500

    # Generate URLs
    original_url = f"https://{SOURCE_BUCKET}.s3.amazonaws.com/{source_key}"
    resized_key = f"resized-{source_key}"
    resized_url = f"https://{TARGET_BUCKET}.s3.amazonaws.com/{resized_key}"

    return jsonify({
        'original_url': original_url,
        'resized_url': resized_url,
        'resized_key': resized_key,
    })

@app.route('/check-resized', methods=['POST'])
@login_required
def check_resized_image():
    resized_key = request.json['resized_key']
    source_key = resized_key.replace("resized-", "")

    try:
        # Check if the resized image exists in the target bucket
        s3.head_object(Bucket=TARGET_BUCKET, Key=resized_key)
        
        # Get the sizes of both the original and resized images
        original_obj = s3.head_object(Bucket=SOURCE_BUCKET, Key=source_key)
        resized_obj = s3.head_object(Bucket=TARGET_BUCKET, Key=resized_key)
        original_size = original_obj['ContentLength']
        resized_size = resized_obj['ContentLength']

        return jsonify({'exists': True, 'original_size': original_size, 'resized_size': resized_size})
    except s3.exceptions.ClientError:
        return jsonify({'exists': False})

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=5000)
