from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from functools import wraps
import random
import json
import csv
import io
import os
from math import floor

# PDF Export imports (optional dependency)
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

app = Flask(__name__)
app.config['SECRET_KEY'] = 'smart-budget-secret-key-2024'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///budget.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.permanent_session_lifetime = timedelta(days=30)

db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(50), default='user')
    joined_date = db.Column(db.DateTime, default=datetime.utcnow)
    budget_daily = db.Column(db.Float, default=0)
    budget_weekly = db.Column(db.Float, default=0)
    budget_monthly = db.Column(db.Float, default=0)
    badges = db.Column(db.String(1000), default="[]")
    challenges = db.Column(db.String(1000), default="[]") # Track claimed challenges

class BankAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    name = db.Column(db.String(100))
    initial_balance = db.Column(db.Float, default=0)
    balance = db.Column(db.Float, default=0)
    type = db.Column(db.String(50))

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount = db.Column(db.Float)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    description = db.Column(db.String(200))
    category = db.Column(db.String(50))
    mood = db.Column(db.String(50))
    payment_method = db.Column(db.String(50))
    bank_id = db.Column(db.Integer, db.ForeignKey('bank_account.id'), nullable=True)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or not session['user_id']:
            flash('Please login first', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or not session['user_id']:
            flash('Please login first', 'danger')
            return redirect(url_for('login'))
        user = User.query.get(session['user_id'])
        if not user:
            flash('Invalid session. Please login again', 'danger')
            session.clear()
            return redirect(url_for('login'))
        if user.role != 'admin':
            flash('Admin access required', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    """Safely get current user or redirect to login"""
    if 'user_id' not in session or not session['user_id']:
        return None
    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return None
    return user

def _load_badges(user: User):
    try:
        return json.loads(user.badges or "[]")
    except Exception:
        return []

def _save_badges(user: User, badges):
    user.badges = json.dumps(badges)
    db.session.add(user)

def evaluate_achievements(user: User):
    """Logic for the 10 specific badges required by the user"""
    badges = {b["id"]: b for b in _load_badges(user)}
    expenses = Expense.query.filter_by(user_id=user.id).order_by(Expense.date.asc()).all()
    total_count = len(expenses)
    today = datetime.utcnow().date()
    days_with_expense = {e.date.date() for e in expenses}

    def add_badge(bid, name, level):
        if bid not in badges:
            badges[bid] = {"id": bid, "name": name, "earned": True, "claimed": False, "level": level, "earned_date": datetime.utcnow().strftime("%Y-%m-%d")}
            flash(f"Badge Unlocked: {name} 🎉", "success")
            return True
        return False

    # 1. Beginner: First expense added
    if total_count >= 1:
        add_badge("beginner", "Beginner", "Bronze")

    # 2. Saver: Under 50% of monthly budget (if budget set and at least 1 expense)
    if user.budget_monthly > 0 and total_count > 0:
        month_start = today.replace(day=1)
        month_total = sum(e.amount for e in expenses if e.date.date() >= month_start)
        if month_total <= user.budget_monthly * 0.5:
            add_badge("saver", "Saver", "Silver")

    # 3. Consistent User: Logged for 12 out of last 15 days
    last_15 = sum(1 for i in range(15) if (today - timedelta(days=i)) in days_with_expense)
    if last_15 >= 12:
        add_badge("consistent_user", "Consistent User", "Silver")

    # 4. Budget Master: Stayed under monthly budget for 3 consecutive months
    # (Simplified: check last 90 days if monthly budget was set and respected)
    if user.budget_monthly > 0:
        over_budget = any(sum(e.amount for e in expenses if (today - timedelta(days=i*30+30)) <= e.date.date() < (today - timedelta(days=i*30))) > user.budget_monthly for i in range(3))
        if not over_budget and total_count > 10: # ensure some activity
            add_badge("budget_master", "Budget Master", "Gold")

    # 5. Expense Tracker Pro: 100+ expenses logged
    if total_count >= 100:
        add_badge("tracker_pro", "Expense Tracker Pro", "Gold")

    # 6. Smart Spender: 50+ expenses while staying under budget
    if total_count >= 50:
        if user.budget_monthly > 0:
            month_total = sum(e.amount for e in expenses if e.date.date() >= today.replace(day=1))
            if month_total <= user.budget_monthly:
                add_badge("smart_spender", "Smart Spender", "Silver")

    # 7. Daily Tracker: Logged expense today
    if today in days_with_expense:
        add_badge("daily_tracker", "Daily Tracker", "Bronze")

    # 8. Weekly Champion: Logged every day for the last 7 days
    last_7 = sum(1 for i in range(7) if (today - timedelta(days=i)) in days_with_expense)
    if last_7 >= 7:
        add_badge("weekly_champion", "Weekly Champion", "Silver")

    # 9. Monthly Master: Logged for at least 25 days in the last 30 days
    last_30_count = sum(1 for i in range(30) if (today - timedelta(days=i)) in days_with_expense)
    if last_30_count >= 25:
        add_badge("monthly_master", "Monthly Master", "Gold")

    # 10. Finance Expert: 200+ expenses logged
    if total_count >= 200:
        add_badge("finance_expert", "Finance Expert", "Platinum")

    badges_list = list(badges.values())
    _save_badges(user, badges_list)
    db.session.commit()
    return badges_list

def _load_challenges(user: User):
    try:
        return json.loads(user.challenges or "[]")
    except Exception:
        return []

def _save_challenges(user: User, challenges):
    user.challenges = json.dumps(challenges)
    db.session.add(user)

def evaluate_challenges(user: User):
    """Logic for the 10 specific challenges required by the user"""
    claimed = _load_challenges(user)
    expenses = Expense.query.filter_by(user_id=user.id).order_by(Expense.date.asc()).all()
    total_count = len(expenses)
    today = datetime.utcnow().date()
    days_with_expense = {e.date.date() for e in expenses}
    
    # Challenge list with targets and current progress
    challenges_data = [
        {"id": "add_5", "name": "Add 5 expenses", "target": 5, "current": total_count},
        {"id": "stay_under_3", "name": "Stay under budget for 3 days", "target": 3, "current": 0},
        {"id": "no_spending", "name": "No spending day", "target": 1, "current": 0},
        {"id": "track_7", "name": "Track expenses for 7 days", "target": 7, "current": len(days_with_expense)},
        {"id": "daily_5", "name": "Add expenses daily for 5 days", "target": 5, "current": 0},
        {"id": "below_50_budget", "name": "Spend below 50% budget", "target": 1, "current": 0},
        {"id": "analytics_use", "name": "Use analytics section", "target": 3, "current": 0}, # Placeholder, track session or page views? 
        {"id": "consistency_week", "name": "Maintain consistency for a week", "target": 7, "current": 0},
        {"id": "bank_use", "name": "Add bank account and use it", "target": 1, "current": 0},
        {"id": "monthly_tracking", "name": "Complete monthly tracking", "target": 30, "current": len(days_with_expense)}
    ]

    # Stay under budget for 3 days
    if user.budget_daily > 0:
        daily_totals = {}
        for e in expenses:
            k = e.date.date()
            daily_totals[k] = daily_totals.get(k, 0) + e.amount
        under_days = 0
        for i in range(7):
            d = today - timedelta(days=i)
            total = daily_totals.get(d, 0)
            if total > 0 and total <= user.budget_daily:
                under_days += 1
        challenges_data[1]["current"] = under_days

    # No spending day (check if any day in last 7 has no expenses)
    for i in range(7):
        if (today - timedelta(days=i)) not in days_with_expense:
            challenges_data[2]["current"] = 1
            break

    # Daily 5 (consecutive days)
    streak = 0
    for i in range(30):
        if (today - timedelta(days=i)) in days_with_expense:
            streak += 1
        else:
            if streak >= 5: break
            streak = 0
    challenges_data[4]["current"] = streak

    # Below 50% budget (monthly)
    if user.budget_monthly > 0:
        month_total = sum(e.amount for e in expenses if e.date.date() >= today.replace(day=1))
        if month_total > 0 and month_total <= user.budget_monthly * 0.5:
            challenges_data[5]["current"] = 1

    # Analytics use (simulated for now)
    challenges_data[6]["current"] = 3 # Always completed for demo or needs session tracking

    # Consistency for a week
    streak = 0
    for i in range(7):
        if (today - timedelta(days=i)) in days_with_expense: streak += 1
    challenges_data[7]["current"] = streak

    # Bank account and use
    has_bank = BankAccount.query.filter_by(user_id=user.id).first()
    used_bank = any(e.bank_id for e in expenses)
    if has_bank and used_bank:
        challenges_data[8]["current"] = 1

    # Result list with status
    results = []
    for ch in challenges_data:
        is_completed = ch["current"] >= ch["target"]
        is_claimed = ch["id"] in claimed
        results.append({
            "id": ch["id"],
            "name": ch["name"],
            "target": ch["target"],
            "current": min(ch["current"], ch["target"]),
            "completed": is_completed,
            "claimed": is_claimed
        })
    
    return results

def predict_category(text):
    text = text.lower()
    if any(word in text for word in ['food',"curd", 'lunch', 'dinner', 'burger', 'pizza', 'eat',"coffee","chips","oreo", "minutemaid","tea", "snacks", "restaurant bill", "grocery shopping", "dosa", "puffs", "biryani", "fried rice", "idli", "bonda", "vada", "dal rice", "puri", "pasta", "upma", "maggie", "samosa", "kachori", "panipuri", "pav bhaji", "burgers", "cheeseburgers", "French fries", "fried chicken", "chicken nuggets", "hot dogs", "pizza", "shawarma", "tacos", "fried momos", "potato chips", "nachos", "cheese balls", "corn puffs", "instant noodles", "samosa", "pakora", "spring rolls", "onion rings", "buttered popcorn", "milk chocolate", "candy bars", "gummies", "lollipops", "caramel candies", "marshmallows", "toffees", "sweetened peanut butter", "chocolate spreads", "chocolate","sugary cereals", "cream biscuits", "cakes", "pastries", "donuts", "brownies", "cupcakes", "ice cream", "milkshakes", "waffles", "pancakes", "soft drinks", "energy drinks", "packaged fruit juices", "flavored milk", "bubble tea", "sweetened iced tea", "cold coffee", "slushies", "sports drinks", "sweet lassi", "parota", "milk", "curd", "biscuit"]): return 'Food'
    elif any(word in text for word in ['uber', 'bus', 'fuel', 'taxi', 'train', 'petrol',"petrol", "diesel", "bus ticket", "metro pass", "auto fare", "cab booking", "Bus", "Train", "Metro", "Car", "Bike", "Bicycle", "Scooter", "Auto rickshaw", "Taxi", "Cab", "Truck", "Van", "Airplane", "Helicopter", "Ship", "Boat", "Ferry", "Subway", "Tram", "Electric scooter"]): return 'Transport'
    elif any(word in text for word in ['amazon', 'shop', 'clothes', 'shirt', 'buy','trouser','kurti','short kurti','shortkurti','jeans']): return 'Shopping'
    elif any(word in text for word in ['electric', 'water', 'internet', 'rent', 'bill']): return 'Bills'
    elif any(word in text for word in ['movie', 'game', 'netflix', 'party', 'fun']): return 'Entertainment'
    elif any(word in text for word in ["electricity bill", "water bill", "gas bill", "internet recharge", "DTH recharge", "Power backup charges", "Generator fuel", "Inverter battery replacement", "Solar maintenance fee", "Prepaid electricity recharge", "Smart meter charges", "Pipeline maintenance fee", "Society maintenance utilities", "Streetlight tax", "Property utility tax", "Water tanker charges", "Borewell maintenance", "Drainage cleaning fee", "Service connection charges", "Late payment penalty"]): return 'Utilities'
    elif any(word in text for word in ["doctor visit", "medicine purchase", "hospital fees", "lab test", "dental checkup", "Doctor consultation", "Hospital charges", "Surgery fees", "Medical tests", "Blood tests", "Scan charges", "X-ray", "MRI scan", "CT scan", "Medicines", "Pharmacy bill", "Vaccination", "Health checkup", "Dental treatment", "Eye checkup", "Physiotherapy", "Emergency care", "Ambulance charges", "Health insurance premium", "Medical equipment"]): return 'Healthcare'
    elif any(word in text for word in ["gym membership", "yoga class", "zumba class", "sports equipment", "protein powder", "Gym membership", "Personal trainer", "Yoga classes", "Zumba classes", "Dance classes", "Fitness app subscription", "Protein powder", "Supplements", "Sports shoes", "Workout clothes", "Dumbbells", "Resistance bands", "Treadmill", "Cycling equipment", "Swimming classes", "Martial arts training", "Cricket coaching", "Badminton coaching", "Fitness tracker", "Sports club fee"]): return 'Fitness'
    elif any(word in text for word in ["rent payment", "house maintenance", "plumbing repair", "painting charges", "furniture purchase", "House rent", "Home loan EMI", "Security deposit", "Property tax", "House maintenance", "Apartment maintenance fee", "Repair charges", "Painting cost", "Furniture purchase", "Home appliances", "Interior decoration", "Plumbing repairs", "Electrical repairs", "Water tank cleaning", "Pest control", "Home insurance", "Renovation expenses", "Brokerage fee", "Moving charges", "Storage charges"]): return 'Housing'
    elif any(word in text for word in ["birthday gift", "anniversary gift", "festival gift", "wedding gift", "gift card", "Birthday gift", "Wedding gift", "Anniversary gift", "Festival gift", "Housewarming gift", "Baby shower gift", "Graduation gift", "Valentines gift", "Return gift", "Corporate gift", "Gift card", "Gift voucher", "Flower bouquet", "Chocolate box", "Customized gift", "Surprise gift", "Farewell gift", "Thank you gift", "Get well soon gift", "New year gift"]): return 'Gifts'
    elif any(word in text for word in ["mobile recharge", "new phone", "phone repair", "screen replacement", "phone accessories", "Mobile recharge", "Postpaid bill", "Data pack", "Top-up recharge", "International roaming", "SIM card", "eSIM activation", "New mobile phone", "Screen replacement", "Battery replacement", "Mobile repair", "Phone case", "Screen guard", "Mobile insurance", "Cloud storage subscription", "App purchase", "In-app purchase", "Caller tune subscription", "Streaming app subscription", "Upgrade charges"]): return 'Mobile'
    elif any(word in text for word in ["flight ticket", "train ticket", "hotel booking", "resort booking", "tour package", "Flight ticket", "Train ticket", "Bus ticket", "Cab fare", "Taxi fare", "Fuel charges", "Toll fee", "Parking fee", "Hotel booking", "Resort stay", "Hostel booking", "Travel insurance", "Visa fee", "Passport charges", "Luggage charges", "Car rental", "Bike rental", "Tour package", "Cruise booking", "Travel agency fee"]): return 'Travel'
    elif any(word in text for word in ["insurance premium", "car insurance", "health insurance", "policy renewal", "Health insurance", "Life insurance", "Term insurance", "Vehicle insurance", "Two-wheeler insurance", "Car insurance", "Travel insurance", "Home insurance", "Property insurance", "Accident insurance", "Critical illness cover", "Family floater policy", "Crop insurance", "Business insurance", "Fire insurance", "Marine insurance", "Insurance premium", "Policy renewal", "Claim processing fee", "Third-party insurance"]): return 'Insurance'
    elif any(word in text for word in ["salon visit", "haircut", "spa treatment", "makeup items", "cosmetics", "Haircut", "Hair spa", "Shampoo", "Conditioner", "Hair oil", "Soap", "Body wash", "Face wash", "Moisturizer", "Sunscreen", "Deodorant", "Perfume", "Makeup products", "Lipstick", "Foundation", "Skincare products", "Nail care", "Shaving kit", "Salon charges", "Spa treatment"]): return 'Personal care'
    elif any(word in text for word in ["school fees", "college tuition", "exam registration", "books purchase", "School fees", "College tuition", "Exam fees", "Admission fees", "Coaching classes", "Online course subscription", "Certification fees", "Books", "Notebooks", "Stationery", "Lab fees", "Library fees", "Project materials", "Uniform", "Hostel fees", "Transportation fees", "Workshop fee", "Seminar fee", "Educational software", "Study materials"]): return 'Education'
    elif any(word in text for word in ["SIP investment", "mutual fund deposit", "stock purchase", "gold investment", "Home loan EMI", "Education loan EMI", "Personal loan EMI", "Car loan EMI", "Two-wheeler loan EMI", "Gold loan", "Loan processing fee", "Foreclosure charges", "Late payment penalty", "Interest payment", "Credit card bill", "Minimum due payment", "Overdraft charges", "Loan insurance", "Prepayment charges", "Business loan EMI", "Mortgage payment", "Microfinance loan", "Borrowing charges", "Debt repayment"]): return 'Investments'
    elif any(word in text for word in ["home loan EMI", "personal loan EMI", "credit card bill", "Mutual funds", "SIP contribution", "Stocks", "Bonds", "Fixed deposit", "Recurring deposit", "Public Provident Fund", "National Pension Scheme", "Gold investment", "Real estate investment", "Cryptocurrency", "Exchange traded funds", "Treasury bills", "Corporate bonds", "Index funds", "Dividend income", "Capital gains", "Portfolio management fee", "Brokerage charges", "Retirement fund"]): return 'Loan'
    elif any(word in text for word in ["Netflix subscription", "Amazon Prime", "Spotify premium", "youtube premium", "aha premium", "disney hotstar subscirption", "prime subscription", "zoho subscription", "OTT subscription", "Music streaming subscription", "Cloud storage subscription", "Gym membership", "Yoga membership", "Magazine subscription", "Newspaper subscription", "Software license subscription", "Antivirus subscription", "VPN subscription", "Online course subscription", "Coding platform subscription", "Meditation app subscription", "Diet plan subscription", "Meal delivery subscription", "Milk delivery subscription", "Water can subscription", "Internet broadband plan", "Mobile postpaid plan", "DTH subscription"]): return 'subscription'
    elif any(word in text for word in ["baby diapers", "school uniform", "kids toys", "School fees", "Tuition classes", "Daycare fees", "Baby food", "Diapers", "Baby clothes", "Toys", "School supplies", "School uniform", "Books", "Vaccination", "Pediatric checkup", "Extracurricular classes", "Sports coaching", "Birthday party expenses", "School trip fee", "Pocket money", "Child insurance", "Educational apps", "Child savings plan"]): return 'kids'
    elif any(word in text for word in ["temple donation", "charity donation", "NGO contribution", "Temple donation", "Mosque donation", "Church offering", "Charity contribution", "NGO donation", "Crowdfunding support", "Disaster relief fund", "Food donation", "Clothes donation", "Blood donation camp", "Scholarship fund", "Orphanage donation", "Old age home donation", "Animal shelter donation", "Medical aid fund", "Community fund", "Zakat", "Tithe", "Fundraising contribution", "Voluntary contribution"]): return 'Donation'
    elif any(word in text for word in ["laptop repair", "software purchase", "antivirus renewal", "Laptop", "Desktop", "Tablet", "Smartwatch", "Headphones", "Earbuds", "Keyboard", "Mouse", "Printer", "Router", "External hard drive", "USB drive", "Software subscription", "Antivirus subscription", "Cloud storage", "Domain purchase", "Web hosting", "Graphics card", "RAM upgrade", "Tech support service"]): return 'Technology'
    elif any(word in text for word in ["AC repair", "water purifier service", "electric repair", "Vehicle servicing", "Oil change", "Tyre replacement", "Car wash", "Bike servicing", "AC servicing", "Refrigerator repair", "Washing machine repair", "Laptop repair", "Mobile repair", "Plumbing repair", "Electrical repair", "Painting touch-up", "Pest control", "Home cleaning service", "Garden maintenance", "Water purifier service", "Generator servicing", "Elevator maintenance", "Annual maintenance contract"]): return 'Maintanence'
    elif any(word in text for word in ["shopping", "scraves", "burqa", "tshirts", "pants", "kurtis", "short kurtis", "pajamas", "palazo", "nightwear", "shoes", "chappals", "slides", "slippers", "flats", "socks", "sweater", "shorts", "kerchief", "tops","shopping"]): return 'Shopping'
    elif any(word in text for word in ["mountaindew", "champa", "sprite", "cococola", "mazaa", "thumsup", "fizz", "pulpyorange", "limca", "7up","minutemate"]): return 'Colddrinks'
    elif any(word in text for word in ["soap","shampoo","surf","paste","xerox","pen","pencil"]): return 'stationary'
    else: return 'Others'

@app.route('/')
def index():
    if 'user_id' in session and session['user_id']:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('Please fill out this field.', 'danger')
            return render_template('login.html')
            
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session.clear()  # Clear any old session
            session.permanent = True
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            flash('Login successful!', 'success')
            return redirect(url_for('admin_panel' if user.role == 'admin' else 'dashboard'))
        else:
            flash('Invalid username or password.', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        role = request.form['role']
        
        if not username or not password or len(password) < 3:
            flash('Please provide valid username and password (min 3 chars)', 'danger')
            return render_template('register.html')
            
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return render_template('register.html')
            
        new_user = User(
            username=username, 
            password=generate_password_hash(password), 
            role=role
        )
        db.session.add(new_user)
        db.session.commit()
        flash('Account created successfully! Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    user = get_current_user()
    if not user:
        flash('Session expired. Please login again', 'danger')
        return redirect(url_for('login'))
    
    # Search & filter for history section
    category_filter = request.args.get('category', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()

    query = Expense.query.filter_by(user_id=user.id)
    if category_filter:
        query = query.filter(Expense.category == category_filter)
    if start_date:
        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.filter(Expense.date >= sd)
        except Exception:
            pass
    if end_date:
        try:
            ed = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(Expense.date < ed)
        except Exception:
            pass
    expenses = query.order_by(Expense.date.desc()).all()
    banks = BankAccount.query.filter_by(user_id=user.id).all()
    
    # Calculate time-based totals
    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    
    # Totals based on all user expenses (not just filtered)
    all_user_expenses = Expense.query.filter_by(user_id=user.id).all()
    daily_expenses = [e for e in all_user_expenses if e.date.date() == today]
    weekly_expenses = [e for e in all_user_expenses if e.date.date() >= week_ago]
    monthly_expenses = [e for e in all_user_expenses if e.date.date() >= month_ago]
    
    total_daily = sum(e.amount for e in daily_expenses)
    total_weekly = sum(e.amount for e in weekly_expenses)
    total_monthly = sum(e.amount for e in monthly_expenses)
    
    alerts = []
    if user.budget_daily > 0 and total_daily > user.budget_daily * 0.9:
        alerts.append("⚠️ Daily budget warning!")
    if user.budget_weekly > 0 and total_weekly > user.budget_weekly * 0.9:
        alerts.append("⚠️ Weekly budget warning!")
    if user.budget_monthly > 0 and total_monthly > user.budget_monthly * 0.9:
        alerts.append("⚠️ Monthly budget warning!")
    if user.budget_monthly > 0 and total_monthly > user.budget_monthly:
        alerts.append("⛔ Monthly budget exceeded!")

    user_badges = evaluate_achievements(user)
    user_challenges = evaluate_challenges(user)
    
    categories = sorted({e.category for e in Expense.query.filter_by(user_id=user.id).all()})

    # Reminders
    reminders = []
    if not daily_expenses:
        reminders.append("Don't forget to add today's expense")
    if user.budget_monthly > 0 and total_monthly > user.budget_monthly * 0.8:
        reminders.append("You are close to exceeding your budget")

    return render_template('dashboard.html', user=user, expenses=expenses, banks=banks, 
                         total_daily=total_daily, total_weekly=total_weekly, total_monthly=total_monthly,
                         alerts=alerts, daily_expenses=daily_expenses, weekly_expenses=weekly_expenses,
                         categories=categories, selected_category=category_filter, start_date=start_date, end_date=end_date,
                         user_badges=user_badges, user_challenges=user_challenges, reminders=reminders)

@app.route('/add_expense', methods=['POST'])
@login_required
def add_expense():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
        
    try:
        amount = float(request.form['amount'])
        desc = request.form['description'].strip()
        mood = request.form['mood']
        payment = request.form['payment_method']
        bank_id = request.form.get('bank_id')
        date_str = request.form.get('date')
        if amount <= 0 or not desc:
            flash('Please enter valid amount and description', 'danger')
            return redirect(url_for('dashboard'))
        try:
            exp_date = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.utcnow()
        except Exception:
            exp_date = datetime.utcnow()
        category = predict_category(desc)
        
        expense = Expense(
            user_id=user.id, 
            amount=amount, 
            date=exp_date,
            description=desc, 
            category=category, 
            mood=mood, 
            payment_method=payment, 
            bank_id=int(bank_id) if bank_id and payment in ['Bank', 'UPI'] else None
        )
        
        if payment in ['Bank', 'UPI'] and bank_id:
            bank = BankAccount.query.get(int(bank_id))
            if bank and bank.user_id == user.id:
                bank.balance -= amount
                db.session.add(bank)
                
        db.session.add(expense)
        db.session.commit()
        evaluate_achievements(user)
        flash('Expense added successfully!', 'success')
    except Exception as e:
        flash('Error adding expense', 'danger')
        db.session.rollback()
    
    return redirect(url_for('dashboard'))

@app.route('/delete_expense/<int:id>')
@login_required
def delete_expense(id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
        
    expense = Expense.query.get(id)
    if expense and expense.user_id == user.id:
        if expense.payment_method in ['Bank', 'UPI'] and expense.bank_id:
            bank = BankAccount.query.get(expense.bank_id)
            if bank and bank.user_id == user.id:
                bank.balance += expense.amount
                db.session.add(bank)
        db.session.delete(expense)
        db.session.commit()
        flash('Expense deleted successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/add_bank', methods=['POST'])
@login_required
def add_bank():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
        
    try:
        name = request.form['name'].strip()
        balance = float(request.form['balance'])
        type_ = request.form['type']
        bank = BankAccount(user_id=user.id, name=name, initial_balance=balance, balance=balance, type=type_)
        db.session.add(bank)
        db.session.commit()
        flash('Bank account added!', 'success')
    except:
        flash('Error adding bank account', 'danger')
        db.session.rollback()
    return redirect(url_for('dashboard'))

@app.route('/edit_bank/<int:id>', methods=['POST'])
@login_required
def edit_bank(id):
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    bank = BankAccount.query.get(id)
    if bank and bank.user_id == user.id:
        try:
            bank.name = request.form['name'].strip()
            # Note: updating balance manually might break virtual tracking consistency, 
            # but user asked for edit capability.
            bank.balance = float(request.form['balance'])
            db.session.commit()
            flash('Bank account updated!', 'success')
        except:
            flash('Error updating bank account', 'danger')
    return redirect(url_for('dashboard'))

@app.route('/delete_bank/<int:id>')
@login_required
def delete_bank(id):
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    bank = BankAccount.query.get(id)
    if bank and bank.user_id == user.id:
        # Check if expenses are linked? Usually better to nullify bank_id in expenses
        Expense.query.filter_by(bank_id=id).update({Expense.bank_id: None})
        db.session.delete(bank)
        db.session.commit()
        flash('Bank account deleted!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/claim_challenge', methods=['POST'])
@login_required
def claim_challenge():
    user = get_current_user()
    if not user: return jsonify({'status': 'error'})
    data = request.get_json()
    ch_id = data.get('id')
    
    # Logic to check if completed and not already claimed
    results = evaluate_challenges(user)
    challenge = next((c for c in results if c["id"] == ch_id), None)
    
    if challenge and challenge["completed"] and not challenge["claimed"]:
        claimed = _load_challenges(user)
        claimed.append(ch_id)
        _save_challenges(user, claimed)
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'Challenge Completed & Claimed'})
    
    return jsonify({'status': 'error', 'message': 'Cannot claim challenge'})

@app.route('/export_pdf')
@login_required
def export_pdf():
    if not REPORTLAB_AVAILABLE:
        flash('PDF Export requires "reportlab" library. Please install it using: pip install reportlab', 'danger')
        return redirect(url_for('dashboard'))
        
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    
    # Reuse filter logic
    category_filter = request.args.get('category', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()

    query = Expense.query.filter_by(user_id=user.id)
    if category_filter:
        query = query.filter(Expense.category == category_filter)
    if start_date:
        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.filter(Expense.date >= sd)
        except: pass
    if end_date:
        try:
            ed = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(Expense.date < ed)
        except: pass
    expenses = query.order_by(Expense.date.asc()).all()

    # Generate PDF
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()
    
    elements.append(Paragraph(f"Expense Report - {user.username}", styles['Title']))
    elements.append(Spacer(1, 12))
    
    # Summary
    total = sum(e.amount for e in expenses)
    elements.append(Paragraph(f"Total Expenses: ₹{total:.2f}", styles['Heading2']))
    elements.append(Spacer(1, 12))
    
    # Date-wise breakdown
    date_totals = {}
    for e in expenses:
        d = e.date.strftime("%Y-%m-%d")
        date_totals[d] = date_totals.get(d, 0) + e.amount
    
    summary_data = [["Date", "Total Amount"]]
    for d, t in sorted(date_totals.items()):
        summary_data.append([d, f"₹{t:.2f}"])
    
    summary_table = Table(summary_data)
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    elements.append(Paragraph("Date-wise Summary", styles['Heading3']))
    elements.append(summary_table)
    elements.append(Spacer(1, 24))
    
    # Full list
    table_data = [["Date", "Description", "Category", "Amount", "Method"]]
    for e in expenses:
        table_data.append([
            e.date.strftime("%Y-%m-%d"),
            e.description[:30],
            e.category,
            f"₹{e.amount:.2f}",
            e.payment_method
        ])
    
    expense_table = Table(table_data, colWidths=[80, 180, 100, 80, 80])
    expense_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.dodgerblue),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey)
    ]))
    elements.append(Paragraph("Detailed Expense List", styles['Heading3']))
    elements.append(expense_table)
    
    doc.build(elements)
    buffer.seek(0)
    return Response(buffer, mimetype='application/pdf', 
                    headers={'Content-Disposition': 'attachment; filename=expenses.pdf'})

@app.route('/update_budget', methods=['POST'])
@login_required
def update_budget():
    user = get_current_user()
    if not user:
        return jsonify({'status': 'error', 'message': 'Unauthorized'})
        
    try:
        data = request.get_json()
        if data['type'] == 'daily':
            user.budget_daily = float(data['value'])
        elif data['type'] == 'weekly':
            user.budget_weekly = float(data['value'])
        elif data['type'] == 'monthly':
            user.budget_monthly = float(data['value'])
        db.session.commit()
        flash('Budget updated successfully!', 'success')
        return jsonify({'status': 'success'})
    except:
        return jsonify({'status': 'error'})

@app.route('/claim_achievement', methods=['POST'])
@login_required
def claim_achievement():
    user = get_current_user()
    if not user:
        return jsonify({'status': 'error'})
    data = request.get_json()
    ach_id = data.get('id')
    badges = _load_badges(user)
    updated = False
    for b in badges:
        if b.get('id') == ach_id and b.get('earned'):
            b['claimed'] = True
            updated = True
            break
    if updated:
        _save_badges(user, badges)
        db.session.commit()
        return jsonify({'status': 'success'})
    return jsonify({'status': 'error'})

def _range_bounds(user_id: int, range_key: str):
    today = datetime.utcnow().date()
    if range_key == 'daily':
        start = datetime.combine(today, datetime.min.time())
        end = start + timedelta(days=1)
    elif range_key == 'weekly':
        start = datetime.combine(today - timedelta(days=6), datetime.min.time())
        end = datetime.combine(today + timedelta(days=1), datetime.min.time())
    else:
        start = datetime.combine(today - timedelta(days=29), datetime.min.time())
        end = datetime.combine(today + timedelta(days=1), datetime.min.time())
    q = Expense.query.filter(Expense.user_id == user_id, Expense.date >= start, Expense.date < end)
    return start, end, q

@app.route('/api/dashboard_data')
@login_required
def dashboard_data():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Unauthorized'})
    
    # Allow admin to specify user_id
    target_user_id = user.id
    requested_user_id = request.args.get('user_id', type=int)
    if requested_user_id and user.role == 'admin':
        target_user_id = requested_user_id

    range_key = request.args.get('range', 'monthly')
    start, end, q = _range_bounds(target_user_id, range_key)
    rows = q.all()
    total = sum(e.amount for e in rows)
    # Category totals
    categories = {}
    for e in rows:
        categories[e.category] = categories.get(e.category, 0) + e.amount
    
    # Bank totals for analytics
    bank_spending = {}
    bank_ids = set(e.bank_id for e in rows if e.bank_id)
    banks = {b.id: b.name for b in BankAccount.query.filter(BankAccount.id.in_(bank_ids)).all()} if bank_ids else {}
    for e in rows:
        if e.bank_id:
            bname = banks.get(e.bank_id, "Unknown Bank")
            bank_spending[bname] = bank_spending.get(bname, 0) + e.amount

    # Line series (daily points or hourly for daily)
    labels = []
    data = []
    if range_key == 'daily':
        # 24 hours
        buckets = [0] * 24
        for e in rows:
            buckets[e.date.hour] += e.amount
        labels = [f"{h:02d}:00" for h in range(24)]
        data = buckets
    else:
        # per day
        days = int((end - start).days)
        buckets = [0] * days
        for e in rows:
            idx = (e.date.date() - start.date()).days
            if 0 <= idx < days:
                buckets[idx] += e.amount
        labels = [(start.date() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        data = buckets
    # Histogram & Scatter data
    spending_frequency = {} # Amount buckets
    spending_patterns = [] # {x: hour, y: amount}
    for e in rows:
        # Histogram: bin by 500s
        bucket = int(e.amount // 500) * 500
        label = f"₹{bucket}-₹{bucket+500}"
        spending_frequency[label] = spending_frequency.get(label, 0) + 1
        
        # Scatter: Hour vs Amount
        spending_patterns.append({'x': e.date.hour, 'y': e.amount})

    return jsonify({
        'total': total,
        'categories': categories,
        'bank_spending': bank_spending,
        'trend': {'labels': labels, 'data': data},
        'frequency': spending_frequency,
        'patterns': spending_patterns
    })

@app.route('/admin')
@admin_required
def admin_panel():
    users = User.query.all()
    all_expenses = Expense.query.order_by(Expense.date.desc()).limit(100).all()
    return render_template('admin.html', users=users, all_expenses=all_expenses)

@app.route('/admin/user/<int:user_id>')
@admin_required
def admin_user_dashboard(user_id):
    user = User.query.get(user_id)
    if not user:
        flash('User not found', 'danger')
        return redirect(url_for('admin_panel'))
    
    expenses = Expense.query.filter_by(user_id=user.id).order_by(Expense.date.desc()).all()
    banks = BankAccount.query.filter_by(user_id=user.id).all()
    
    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    
    daily_expenses = [e for e in expenses if e.date.date() == today]
    weekly_expenses = [e for e in expenses if e.date.date() >= week_ago]
    monthly_expenses = [e for e in expenses if e.date.date() >= month_ago]
    
    total_daily = sum(e.amount for e in daily_expenses)
    total_weekly = sum(e.amount for e in weekly_expenses)
    total_monthly = sum(e.amount for e in monthly_expenses)
    
    # Update and fetch user achievements for the admin view
    user_badges = evaluate_achievements(user)
    user_challenges = evaluate_challenges(user)
    all_users = User.query.filter(User.role != 'admin').all()
    
    # Get expenses grouped by bank for MY ACCOUNTS section
    bank_transactions = {}
    for bank in banks:
        bank_transactions[bank.id] = Expense.query.filter_by(user_id=user.id, bank_id=bank.id).order_by(Expense.date.desc()).all()

    return render_template('admin_user_dashboard.html', user=user, expenses=expenses, banks=banks,
                         total_daily=total_daily, total_weekly=total_weekly, total_monthly=total_monthly,
                         user_badges=user_badges, user_challenges=user_challenges, all_users=all_users,
                         bank_transactions=bank_transactions)

@app.route('/admin/delete_user/<int:id>')
@admin_required
def delete_user(id):
    user = User.query.get(id)
    if user and user.role != 'admin':
        Expense.query.filter_by(user_id=id).delete()
        BankAccount.query.filter_by(user_id=id).delete()
        db.session.delete(user)
        db.session.commit()
        flash('User deleted successfully!', 'success')
    else:
        flash('Cannot delete admin account!', 'danger')
    return redirect(url_for('admin_panel'))

@app.route('/api/analytics/<int:user_id>')
def analytics(user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'})
    expenses = Expense.query.filter_by(user_id=user.id).all()
    cat_data = {}
    for e in expenses:
        cat_data[e.category] = cat_data.get(e.category, 0) + e.amount
    return jsonify({'categories': cat_data, 'health_score': random.randint(60, 95)})

@app.route('/api/analytics')
@login_required
def user_analytics():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Unauthorized'})
    return analytics(user.id)

def migrate_db():
    """Manual migration to handle schema changes without dropping data"""
    from sqlalchemy import text
    with app.app_context():
        # Add initial_balance to bank_account
        try:
            db.session.execute(text("ALTER TABLE bank_account ADD COLUMN initial_balance FLOAT DEFAULT 0"))
            db.session.commit()
            print("✅ Added initial_balance to bank_account")
        except:
            db.session.rollback()

        # Add challenges to user
        try:
            db.session.execute(text("ALTER TABLE user ADD COLUMN challenges VARCHAR(1000) DEFAULT '[]'"))
            db.session.commit()
            print("✅ Added challenges to user")
        except:
            db.session.rollback()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        migrate_db()
        # Create default admin if not exists
        if not User.query.filter_by(username='admin').first():
            admin_user = User(
                username='admin', 
                password=generate_password_hash('admin123'), 
                role='admin'
            )
            db.session.add(admin_user)
            db.session.commit()
            print("✅ Default admin created: username='admin', password='admin123'")
    
    print("Server starting on http://127.0.0.1:5000")
    print("📱 Default Admin: admin / admin123")
    app.run(debug=True, host='127.0.0.1')
