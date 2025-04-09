# InstantLegal AI - Legal Document Generator

A Flask web application that generates professional legal documents using OpenAI's GPT-4 API and ReportLab for PDF generation.

## Features

- AI-powered legal document generation
- Multiple document types (NDA, Terms of Service, Privacy Policy, etc.)
- Customization based on business type, industry, and state
- PDF generation and download
- Responsive web interface

## Tech Stack

- **Backend**: Python 3.10+, Flask
- **AI**: OpenAI GPT-4 API
- **PDF Generation**: ReportLab
- **Frontend**: HTML, CSS, JavaScript (Vanilla)
- **Security**: Flask-Limiter for rate limiting, CORS protection

## Installation

1. Clone the repository:
```
git clone <repository-url>
cd instantlegal-ai
```

2. Create a virtual environment and activate it:
```
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```
pip install -r requirements.txt
```

4. Create a `.env` file in the root directory with the following variables:
```
FLASK_APP=app.py
FLASK_ENV=development
OPENAI_API_KEY=your_openai_api_key_here
SECRET_KEY=your_secret_key_here
```

5. Run the application:
```
flask run
```

6. Open your browser and navigate to `http://localhost:5000`

## Usage

1. Select a document type from the dropdown menu
2. Fill in your business details
3. Choose protection level and any special clauses
4. Click "Generate Document Now"
5. Download your generated PDF document

## Project Structure

```
instantlegal-ai/
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables (not in repo)
├── .gitignore              # Git ignore file
├── README.md               # Project documentation
├── static/                 # Static files
│   ├── css/                # CSS files
│   ├── js/                 # JavaScript files
│   └── documents/          # Generated documents
└── templates/              # HTML templates
    └── index.html          # Main application template
```

## Adding New Translations

1. Install Flask-Babel:
```
pip install Flask-Babel
```

2. Initialize Babel in your Flask app (`app.py`):
```python
from flask_babel import Babel

app = Flask(__name__)
babel = Babel(app)

@babel.localeselector
def get_locale():
    return request.accept_languages.best_match(['en', 'es'])
```

3. Create a `translations` directory in the root of your project:
```
mkdir translations
```

4. Extract messages from your templates and Python files:
```
pybabel extract -F babel.cfg -o messages.pot .
```

5. Initialize a new language (e.g., Spanish):
```
pybabel init -i messages.pot -d translations -l es
```

6. Edit the generated `messages.po` file in `translations/es/LC_MESSAGES/` to add your translations.

7. Compile the translations:
```
pybabel compile -d translations
```

8. Restart your Flask application to apply the new translations.

## Running the App with Multiple Languages on Render

To run the app with multiple languages on Render, follow these steps:

1. Ensure that your `requirements.txt` includes `Flask-Babel`:
```
Flask-Babel==3.0.0
```

2. Make sure your `app.py` is configured to use Flask-Babel as shown in the previous section.

3. Add the following environment variables to your Render service settings:
```
FLASK_APP=app.py
FLASK_ENV=production
OPENAI_API_KEY=your_openai_api_key_here
SECRET_KEY=your_secret_key_here
```

4. Deploy your app on Render. Render will automatically detect the `requirements.txt` and install the necessary dependencies.

5. Once deployed, your app will be accessible with support for multiple languages based on user preferences.

## License

MIT

## Disclaimer

This application is for demonstration purposes only. The generated legal documents should be reviewed by a qualified legal professional before use in a real-world context. 
