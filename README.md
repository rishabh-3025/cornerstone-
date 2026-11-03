🏨 GrandStay Advanced

A Smart Hotel Booking and Management System

GrandStay Advanced is a web-based hotel booking and management application designed to simplify hotel reservations, room management, payments, and customer interactions through a unified platform.

✨ Features

Customer Features

* User Registration and Authentication
* Browse and Explore Available Rooms
* Room Search and Availability Checking
* Online Room Booking
* Dynamic Booking Price Calculation
* Coupon and Discount Application
* Payment Management and UPI Payment Proof Upload
* Booking Confirmation and Cancellation
* Booking History and User Dashboard
* PDF Invoice Generation
* Customer Reviews and Ratings
* Profile Management

Admin Features

* Dedicated Admin Login
* Hotel Management Dashboard
* Booking and Payment Management
* Payment Proof Verification
* Room Price Management
* Coupon Creation and Management
* Customer Booking Monitoring

🛠️ Technology Stack

Component	Technology
Backend	Python, Flask
Frontend	HTML, CSS
Database	SQLite
Authentication	Werkzeug Password Hashing
Image Processing	Pillow
PDF Generation	ReportLab
Version Control	Git and GitHub

📂 Project Structure

GrandStay_Advanced/
│
├── app.py
├── grandstay.db
├── requirements.txt
├── README.md
│
├── templates/
│   ├── home.html
│   ├── rooms.html
│   ├── login.html
│   └── ...
│
├── static/
│   ├── css/
│   ├── images/
│   └── ...
│
└── instance/
    └── payment_proofs/

The structure above illustrates the main application components.

⚙️ Installation and Setup

1. Clone the Repository

git clone https://github.com/Rishabh-3025/cornerstone-.git

2. Navigate to the Project Directory

cd cornerstone-

3. Create a Virtual Environment

python3 -m venv venv

Activate it:

Mac/Linux:

source venv/bin/activate

Windows:

venv\Scripts\activate

4. Install Dependencies

pip install -r requirements.txt

5. Run the Application

python app.py

6. Open in Browser

http://127.0.0.1:5000

🔐 Security

The application uses password hashing for user authentication.

For production deployment:

* Use environment variables for secret keys.
* Avoid committing credentials or sensitive information.
* Configure persistent database storage.
* Secure uploaded payment documents.

🚀 Future Enhancements

* Integration with secure online payment gateways.
* Email and SMS booking notifications.
* Advanced analytics and reporting.
* Improved mobile responsiveness.
* Cloud-based database integration.
* Enhanced room recommendation system.

👩‍💻 Contributors

Developed collaboratively as a hotel booking and management system project.

📄 License

This project is developed for educational and demonstration purposes.
