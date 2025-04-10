import os
import json
import uuid
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from openai import OpenAI
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib import colors
import stripe
import time
from flask_babel import Babel, _
import geoip2.database
from geoip2.errors import AddressNotFoundError
import requests # Still needed for MaxMind DB download
import tarfile # Still needed for MaxMind DB download
import shutil  # Still needed for MaxMind DB download
import pycountry # <--- Added for jurisdictions

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "default-secret-key")

# Configure CORS
CORS(app)

# Configure rate limiting
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# Configure Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

# Validate API keys
if not stripe.api_key:
    app.logger.warning("Stripe API key not set. Stripe functionality will not work.")
if not STRIPE_PUBLISHABLE_KEY:
    app.logger.warning("Stripe publishable key not set. Stripe checkout will not work.")

# Configure OpenAI API key
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    app.logger.warning("OpenAI API key not set. Document generation will not work.")

# Initialize OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Define base directories relative to the app's root path
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_FOLDER = os.path.join(APP_ROOT, "static")
UPLOAD_FOLDER = os.path.join(STATIC_FOLDER, "documents")
DOWNLOAD_FOLDER = os.path.join(STATIC_FOLDER, "downloads")
TRANSLATIONS_FOLDER = os.path.join(APP_ROOT, 'translations')
GEOLITE2_DB_PATH = os.path.join(APP_ROOT, 'GeoLite2-Country.mmdb')

# Ensure necessary directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Document types and their descriptions
DOCUMENT_TYPES = {
    "nda": "Non-Disclosure Agreement (NDA)",
    "terms": "Website Terms of Service",
    "privacy": "Privacy Policy",
    "contract": "Freelance Contract",
    "employee": "Employment Agreement",
    "partnership": "Partnership Agreement"
}

# Initialize Babel
babel = Babel(app)

# Configure Babel
app.config['BABEL_DEFAULT_LOCALE'] = 'en'
app.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
app.config['BABEL_TRANSLATION_DIRECTORIES'] = TRANSLATIONS_FOLDER

# Country names for display (can be expanded)
# pycountry can also provide country names: pycountry.countries.get(alpha_2='US').name
COUNTRY_DISPLAY_NAMES = {
    'US': 'United States',
    'GB': 'United Kingdom',
    'ES': 'Spain',
    'FR': 'France',
    'DE': 'Germany',
    'IT': 'Italy',
    'CA': 'Canada',
    'AU': 'Australia',
    'NZ': 'New Zealand',
    'IE': 'Ireland',
    'MX': 'Mexico',
    # Add more or use pycountry dynamically if needed
}

# --- Removed Hardcoded Jurisdiction Data ---
# US_STATES = {...} removed
# fetch_spanish_jurisdictions(), fetch_mexican_jurisdictions(), fetch_german_jurisdictions() removed
# REST_COUNTRIES_BASE_URL removed
# fetch_country_jurisdictions() removed

# Cache for jurisdiction data (using pycountry)
jurisdiction_cache = {}
CACHE_DURATION = timedelta(days=1).total_seconds() # Use timedelta for clarity

# MaxMind GeoLite2 Configuration
MAXMIND_LICENSE_KEY = os.getenv("MAXMIND_LICENSE_KEY")
GEOLITE2_UPDATE_INTERVAL = timedelta(days=30)

def download_geolite2_database():
    """Download and update the GeoLite2 Country database."""
    if not MAXMIND_LICENSE_KEY:
        app.logger.warning("MaxMind license key not set. Geolocation will default to US.")
        return False
    try:
        url = f"https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-Country&license_key={MAXMIND_LICENSE_KEY}&suffix=tar.gz"
        response = requests.get(url, stream=True)
        response.raise_for_status() # Raise an exception for bad status codes

        temp_dir = os.path.join(APP_ROOT, "temp_geodb")
        os.makedirs(temp_dir, exist_ok=True)
        temp_file = os.path.join(temp_dir, "geolite2_country.tar.gz")

        with open(temp_file, 'wb') as f:
            response.raw.decode_content = True
            shutil.copyfileobj(response.raw, f)

        extracted_db_path = None
        with tarfile.open(temp_file, 'r:gz') as tar:
            for member in tar.getmembers():
                if member.name.endswith('.mmdb'):
                    # Extract to temp dir first to avoid overwriting potentially in-use DB
                    tar.extract(member, temp_dir)
                    extracted_db_path = os.path.join(temp_dir, member.name)
                    break # Assume only one .mmdb file

        if extracted_db_path and os.path.exists(extracted_db_path):
            shutil.move(extracted_db_path, GEOLITE2_DB_PATH)
            app.logger.info(f"GeoLite2 database updated successfully to {GEOLITE2_DB_PATH}")
            # Clean up temporary files and directory
            shutil.rmtree(temp_dir)
            return True
        else:
            app.logger.error("Failed to find .mmdb file in downloaded archive.")
            shutil.rmtree(temp_dir) # Clean up even on failure
            return False

    except requests.exceptions.RequestException as e:
         app.logger.error(f"Failed to download GeoLite2 database: {e}")
         return False
    except tarfile.TarError as e:
        app.logger.error(f"Error extracting GeoLite2 database archive: {e}")
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        return False
    except Exception as e:
        app.logger.error(f"Error processing GeoLite2 database: {str(e)}")
        if 'temp_dir' in locals() and os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        return False

def check_and_update_geolite2():
    """Check if the GeoLite2 database exists and is up to date."""
    try:
        if not os.path.exists(GEOLITE2_DB_PATH):
            app.logger.info("GeoLite2 database not found. Attempting download...")
            return download_geolite2_database()

        mod_time = datetime.fromtimestamp(os.path.getmtime(GEOLITE2_DB_PATH))
        if datetime.now() - mod_time > GEOLITE2_UPDATE_INTERVAL:
            app.logger.info("GeoLite2 database is outdated. Updating...")
            return download_geolite2_database()

        app.logger.info("GeoLite2 database is up to date.")
        return True
    except Exception as e:
        app.logger.error(f"Error checking GeoLite2 database status: {str(e)}")
        return False

# Check/Update GeoLite2 DB on startup
check_and_update_geolite2()

# --- Jurisdiction fetching using pycountry ---
def get_jurisdictions_from_library(country_code):
    """
    Gets subdivisions for a country using pycountry, 
    filtering for relevant types (e.g., Autonomous Communities/Cities for Spain).
    """
    try:
        all_subdivisions = pycountry.subdivisions.get(country_code=country_code.upper())
        if not all_subdivisions:
             app.logger.info(f"No subdivisions found in pycountry for {country_code}")
             return {}

        # Debug logging for Spanish jurisdictions
        if country_code.upper() == 'ES':
            app.logger.debug(f"Found {len(list(all_subdivisions))} total subdivisions for Spain")
            app.logger.debug(f"Subdivision types found: {set(sub.type for sub in all_subdivisions)}")

        # --- FILTERING LOGIC ---
        # Define the types you want to include for the jurisdiction dropdown
        # Common types for Spain: 'Autonomous community', 'Autonomous city'
        # You might need to inspect subdivision.type for other countries if needed.
        desired_types = {
            'ES': ['Autonomous community', 'Autonomous city', 'Comunidad autónoma', 'Ciudad autónoma'],
            'US': ['State'],
            'DE': ['Land'],
            'MX': ['State', 'Estado'],
            'FR': ['Metropolitan department', 'Overseas department']
        }.get(country_code.upper(), ['State', 'Province', 'Region'])  # Default types for other countries
        
        filtered_subdivisions = [
            sub for sub in all_subdivisions 
            if any(desired_type.lower() in sub.type.lower() 
                  for desired_type in desired_types)
        ]
        
        if not filtered_subdivisions:
             app.logger.warning(f"No subdivisions of desired types {desired_types} found for {country_code}, even though pycountry returned some subdivisions.")
             # Optional: You could fall back to showing all subdivisions here if desired
             # filtered_subdivisions = all_subdivisions 
             return {} # Return empty if no desired types found

        # Convert the FILTERED list to the dictionary format
        jurisdiction_dict = {}
        for sub in filtered_subdivisions:
            # Special handling for Spanish jurisdictions
            if country_code.upper() == 'ES':
                # Handle special cases for Spanish regions
                name = sub.name
                if ', ' in name:
                    # Convert "Madrid, Comunidad de" to "Comunidad de Madrid"
                    parts = name.split(', ')
                    if len(parts) == 2:
                        name = f"{parts[1]} {parts[0]}"
            else:
                name = sub.name
            
            jurisdiction_dict[sub.code.split('-')[-1]] = name

        # Optional: Log the filtered count
        app.logger.debug(f"Found {len(jurisdiction_dict)} jurisdictions of types {desired_types} for {country_code}")
        
        return jurisdiction_dict

    except KeyError:
        app.logger.warning(f"Country code {country_code} not recognized by pycountry.")
        return {}
    except Exception as e:
        app.logger.error(f"Error fetching/filtering subdivisions for {country_code} from pycountry: {str(e)}")
        return {}

def get_cached_jurisdictions(country_code):
    """
    Get jurisdictions from cache or fetch using pycountry if needed.
    """
    global jurisdiction_cache
    current_time = time.time()

    # Check cache first
    if country_code in jurisdiction_cache:
        cache_time, data = jurisdiction_cache[country_code]
        if current_time - cache_time < CACHE_DURATION:
            # app.logger.debug(f"Using cached jurisdictions for {country_code}")
            return data

    # If not in cache or expired, fetch using pycountry
    app.logger.info(f"Fetching jurisdictions for {country_code} using pycountry.")
    data = get_jurisdictions_from_library(country_code)

    # Update cache (even if data is empty, to avoid refetching immediately)
    jurisdiction_cache[country_code] = (current_time, data)
    # app.logger.debug(f"Cached jurisdictions for {country_code}: {data}")
    return data

def get_visitor_location():
    """Determine visitor's country code using GeoLite2 DB."""
    try:
        if not os.path.exists(GEOLITE2_DB_PATH):
            app.logger.warning("GeoLite2 database not found at {}. Using default 'US' location.".format(GEOLITE2_DB_PATH))
            return 'US'

        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)

        # Handle common local/private IPs
        if ip_address.startswith(('127.', '10.', '172.', '192.168.')) or ip_address == '::1':
             app.logger.debug(f"Local/Private IP detected ({ip_address}). Defaulting to 'ES'.")
             return 'ES' # Default to Spain for testing

        # Use a context manager for the reader
        with geoip2.database.Reader(GEOLITE2_DB_PATH) as reader:
            response = reader.country(ip_address)
            country_code = response.country.iso_code
            app.logger.debug(f"Detected country code {country_code} for IP {ip_address}")
            return country_code

    except AddressNotFoundError:
        app.logger.warning(f"IP address {ip_address} not found in GeoLite2 database. Defaulting to 'US'.")
        return 'US'
    except FileNotFoundError:
         app.logger.error(f"GeoLite2 database file not found at expected path: {GEOLITE2_DB_PATH}")
         return 'US' # Default if file vanished after startup check
    except Exception as e:
        # Catch-all for other potential geoip2 errors or unexpected issues
        app.logger.error(f"Error determining visitor location: {str(e)}")
        return 'US'

def get_jurisdiction(country_code):
    """
    Get jurisdiction options for the dropdown.
    Returns a dictionary of subdivisions or the country name itself if no subdivisions exist.
    """
    jurisdictions = get_cached_jurisdictions(country_code)

    if jurisdictions:
        return jurisdictions

    # If pycountry returned no subdivisions, fallback to the country name
    # Use COUNTRY_DISPLAY_NAMES or fetch dynamically from pycountry
    country_name = COUNTRY_DISPLAY_NAMES.get(country_code)
    if not country_name:
        try:
             country_name = pycountry.countries.get(alpha_2=country_code).name
        except KeyError:
             country_name = country_code # Fallback to code if name not found
        except Exception as e:
             app.logger.error(f"Error getting country name for {country_code} from pycountry: {e}")
             country_name = country_code # Fallback

    app.logger.info(f"No specific subdivisions for {country_code}, using country name '{country_name}' as jurisdiction.")
    return {country_code: country_name} # e.g., {'FR': 'France'}

def get_locale():
    """Determine the locale based on country and browser preferences."""
    country_code = get_visitor_location()

    # Define supported languages within the app
    SUPPORTED_LANGUAGES = ['en', 'es', 'de', 'fr', 'it'] # Add all languages you have translations for

    # Map country codes to preferred languages (can be expanded)
    country_language_map = {
        'ES': 'es', 'MX': 'es', 'AR': 'es', 'CO': 'es', 'PE': 'es', 'CL': 'es', # Spanish-speaking
        'US': 'en', 'GB': 'en', 'AU': 'en', 'CA': 'en', 'NZ': 'en', 'IE': 'en', # English-speaking
        'DE': 'de', 'AT': 'de', 'CH': 'de', # German-speaking (CH also has fr, it)
        'FR': 'fr', 'BE': 'fr', # French-speaking (BE also has nl, de)
        'IT': 'it',              # Italian-speaking
        # Add more mappings
    }

    lang = country_language_map.get(country_code)

    if lang and lang in SUPPORTED_LANGUAGES:
         app.logger.debug(f"Locale determined by country {country_code}: {lang}")
         return lang

    # Fallback to browser preferences, matching against *our* supported languages
    best_match = request.accept_languages.best_match(SUPPORTED_LANGUAGES)
    app.logger.debug(f"Locale determined by Accept-Language header: {best_match}")
    return best_match or app.config['BABEL_DEFAULT_LOCALE'] # Ensure we always return a locale


def get_timezone():
    # Placeholder - Timezone detection is more complex
    return 'UTC'

# --- Assign Babel Selectors ---
babel.init_app(app, locale_selector=get_locale, timezone_selector=get_timezone)


@app.route('/')
def index():
    """Render the main page."""
    country_code = get_visitor_location()
    jurisdictions = get_jurisdiction(country_code)
    current_language = get_locale() # Use the same function Babel uses

    # Get country display name, falling back gracefully
    selected_country_name = COUNTRY_DISPLAY_NAMES.get(country_code)
    if not selected_country_name:
        try:
            selected_country_name = pycountry.countries.get(alpha_2=country_code).name
        except: # Broad except to catch KeyError or other pycountry issues
            selected_country_name = country_code # Fallback to code

    return render_template(
        'index.html',
        document_types=DOCUMENT_TYPES,
        stripe_key=STRIPE_PUBLISHABLE_KEY,
        jurisdictions=jurisdictions,
        selected_country=selected_country_name, # Use the determined name
        country_code=country_code,
        current_language=current_language
    )

# --- Payment Routes (create_checkout_session, payment_return, payment_success) ---
# These remain largely the same as before. Added slight logging improvements.
@app.route('/create-checkout-session', methods=['POST'])
@limiter.limit("10 per minute") # Add rate limiting
def create_checkout_session():
    try:
        form_data = request.form
        doc_type = form_data.get("document_type", "custom")
        doc_name = DOCUMENT_TYPES.get(doc_type, "Custom Document")
        app.logger.info(f"Creating checkout session for document type: {doc_type}")

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'Legal Document: {doc_name}',
                        'description': 'AI-generated legal document tailored to your needs.',
                    },
                    'unit_amount': 9900, # $99.00
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=url_for('payment_return', session_id='{CHECKOUT_SESSION_ID}', _external=True),
            cancel_url=url_for('index', _external=True),
            metadata={'form_data': json.dumps(dict(form_data))}
        )
        app.logger.info(f"Stripe session created: {checkout_session.id}")
        return jsonify({'sessionId': checkout_session.id})

    except stripe.error.StripeError as e:
        app.logger.error(f"Stripe API error during checkout creation: {str(e)}")
        return jsonify({'error': f"Stripe error: {str(e)}"}), 500
    except Exception as e:
        app.logger.error(f"Error creating checkout session: {str(e)}")
        return jsonify({'error': 'An internal error occurred'}), 500

@app.route('/payment-return', methods=['GET'])
def payment_return():
    session_id = request.args.get('session_id')
    if not session_id:
        app.logger.warning("Payment return page accessed without session_id.")
        return redirect(url_for('index'))
    # This page might just show a "processing" message and use JS
    # to poll the /payment-success endpoint.
    return render_template('payment_return.html', session_id=session_id, stripe_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/payment-success', methods=['GET'])
@limiter.limit("5 per minute") # Limit polling frequency
def payment_success():
    session_id = request.args.get('session_id')
    if not session_id:
        app.logger.warning("Payment success endpoint called without session_id.")
        return jsonify({'error': 'Missing session ID'}), 400

    try:
        app.logger.info(f"Verifying payment success for session: {session_id}")
        session = stripe.checkout.Session.retrieve(session_id)

        # IMPORTANT: Verify payment status properly in production
        if session.payment_status != 'paid':
            app.logger.warning(f"Payment not completed for session {session_id}. Status: {session.payment_status}")
            # You might return a 'pending' status or an error depending on your flow
            return jsonify({'status': 'pending', 'message': 'Payment not yet complete.'}), 202

        form_data = json.loads(session.metadata.get('form_data', '{}'))
        if not form_data:
             app.logger.error(f"Missing form_data in metadata for session {session_id}")
             return jsonify({'error': 'Could not retrieve document details.'}), 500

        app.logger.info(f"Payment successful for session {session_id}. Proceeding with document generation.")

        # --- Document Generation Logic (extracted for clarity) ---
        try:
            document_result = generate_document_with_timeout(form_data)
            return jsonify(document_result)
        except TimeoutError:
             app.logger.warning(f"Document generation timed out for session {session_id}.")
             return jsonify({
                'status': 'processing',
                'message': 'Document generation is taking longer than expected. Please check back shortly or contact support.'
             }), 202 # Accepted
        except Exception as doc_error:
             app.logger.error(f"Document generation failed for session {session_id}: {str(doc_error)}")
             # Be careful not to expose sensitive error details to the client
             return jsonify({'error': 'Failed to generate document due to an internal error.'}), 500

    except stripe.error.InvalidRequestError as e:
         app.logger.error(f"Invalid Stripe request for session {session_id}: {str(e)}")
         return jsonify({'error': 'Invalid payment session ID.'}), 404
    except stripe.error.StripeError as e:
        app.logger.error(f"Stripe API error during payment verification: {str(e)}")
        return jsonify({'error': f"Payment verification failed: {str(e)}"}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error in payment success route: {str(e)}")
        return jsonify({'error': 'An unexpected error occurred.'}), 500


def generate_document_with_timeout(form_data):
    """Wrapper for generate_document with timeout and retries."""
    start_time = time.time()
    # Slightly shorter than Heroku/Gunicorn timeout to allow response sending
    timeout_limit = os.getenv("DOC_GEN_TIMEOUT", 28)
    max_retries = 2
    retry_delay = 1

    for attempt in range(max_retries):
        try:
            # Check timeout before each attempt
            if time.time() - start_time > timeout_limit:
                raise TimeoutError("Document generation exceeded time limit")

            app.logger.info(f"Starting document generation attempt {attempt + 1}/{max_retries}")
            # Pass remaining time to generate_document if it supports it,
            # or just rely on the outer timeout check.
            # remaining_time = timeout_limit - (time.time() - start_time)
            result = generate_document(form_data) # Assuming generate_document raises exceptions on failure

            if result.get('success'):
                app.logger.info("Document generation successful.")
                return result
            else:
                 # This case might not be reached if generate_document raises exceptions
                 error_msg = result.get('error', 'Unknown generation error')
                 app.logger.error(f"Document generation attempt {attempt + 1} failed: {error_msg}")
                 # Optionally retry specific non-fatal errors here

        except TimeoutError as te:
             # Rethrow timeout specifically if needed by calling function
             raise te
        except Exception as e:
            app.logger.error(f"Document generation exception (attempt {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay *= 2 # Exponential backoff
            else:
                app.logger.error(f"Document generation failed after {max_retries} attempts.")
                raise Exception(f"Failed to generate document after retries: {str(e)}") # Raise the last error

    # Should not be reached if logic is correct, but acts as a fallback
    return {'success': False, 'error': 'Document generation failed after all retries.'}


# --- Health Check ---
@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    errors = []
    component_status = {'stripe': 'ok', 'openai': 'ok', 'geoip_db': 'ok'}

    # Check Stripe
    try:
        stripe.Account.retrieve()
    except Exception as e:
        component_status['stripe'] = 'error'
        errors.append(f"Stripe connection error: {str(e)}")
        app.logger.error("Health Check: Stripe connection failed.")

    # Check OpenAI
    try:
        # More robust check: list models or make a cheap API call if necessary
        client.models.list(timeout=5)
    except Exception as e:
        component_status['openai'] = 'error'
        errors.append(f"OpenAI connection error: {str(e)}")
        app.logger.error("Health Check: OpenAI connection failed.")

    # Check GeoIP Database
    if not os.path.exists(GEOLITE2_DB_PATH):
         component_status['geoip_db'] = 'error'
         errors.append("GeoLite2 database file missing.")
         app.logger.error("Health Check: GeoIP DB missing.")

    status = 'unhealthy' if errors else 'healthy'
    response = {
        'status': status,
        'timestamp': datetime.now().isoformat(),
        'components': component_status
    }
    if errors:
        response['errors'] = errors

    http_status = 503 if status == 'unhealthy' else 200
    return jsonify(response), http_status


# --- Document Generation (Core Logic) ---
@app.route('/generate-document', methods=['POST'])
@limiter.limit("5 per minute") # Limit direct generation calls if bypassing payment
def handle_document_generation():
    """Handle direct document generation request (e.g., for testing)."""
    if os.getenv("BYPASS_PAYMENT", "false").lower() != "true":
        return jsonify({'error': 'Payment required'}), 402

    try:
        app.logger.info("Handling direct document generation request.")
        # Using the timeout wrapper here as well for consistency
        document_result = generate_document_with_timeout(request.form)
        return jsonify(document_result)

    except TimeoutError:
             app.logger.warning("Direct document generation timed out.")
             return jsonify({
                'status': 'processing',
                'message': 'Document generation is taking longer than expected.'
             }), 202 # Accepted
    except Exception as e:
        app.logger.error(f"Error in direct document generation: {str(e)}")
        return jsonify({'error': f'Failed to generate document: An internal error occurred.'}), 500


def generate_document(form_data):
    """Generates the legal document using OpenAI."""
    try:
        # --- Extract and Validate Form Data ---
        document_type = form_data.get('document_type')
        business_name = form_data.get('business_name')
        business_type = form_data.get('business_type')
        # Use the 'jurisdiction' key if passed, else 'state' for backward compatibility
        jurisdiction = form_data.get('jurisdiction', form_data.get('state'))
        industry = form_data.get('industry')
        protection_level = form_data.get('protection_level', '2')
        additional_instructions = form_data.get('additional_instructions', '')

        if not all([document_type, business_name, business_type, jurisdiction, industry]):
             missing = [k for k, v in locals().items() if k in ['document_type', 'business_name', 'business_type', 'jurisdiction', 'industry'] and not v]
             raise ValueError(f"Missing required form fields: {', '.join(missing)}")

        clauses = [
            label for key, label in [
                ('clause_confidentiality', "Enhanced Confidentiality"),
                ('clause_arbitration', "Arbitration Provision"),
                ('clause_termination', "Advanced Termination Options"),
                ('clause_ip', "Intellectual Property Protection")
            ] if form_data.get(key) # Check if checkbox key exists and is truthy
        ]

        doc_type_name = DOCUMENT_TYPES.get(document_type, 'legal document')

        # --- Construct OpenAI Prompt ---
        prompt = f"""Generate a professional {doc_type_name} for a business named "{business_name}".

Business Details:
- Type: {business_type}
- Industry: {industry}
- Governing Law/Jurisdiction: {jurisdiction} (Ensure the document is tailored for the laws of this jurisdiction)

Document Requirements:
- Protection Level: {protection_level}/3 (Adjust complexity and protective measures accordingly)
- Special Clauses to Include: {', '.join(clauses) if clauses else 'Standard clauses appropriate for this document type'}
- Additional Instructions: {additional_instructions if additional_instructions else 'None'}

Formatting Guidelines:
- Use clear, hierarchical section headings (e.g., using markdown #, ## or bold text).
- Employ standard legal document structure (e.g., Parties, Recitals, Definitions, Operative Clauses, Boilerplate, Signatures).
- Ensure proper spacing and indentation for readability.
- Include placeholders for specific details where necessary (e.g., dates, addresses, specific values).
- Format signature blocks clearly, including lines for signature, printed name, title (if applicable), and date. Example:

  **Party A Signature:** ______________________
  Printed Name: ______________________
  Title: ______________________
  Date: _______________

Output:
- Generate the full text of the legal document.
- Use professional and legally appropriate language, suitable for the specified jurisdiction ({jurisdiction}).
- Ensure clarity for business professionals.
"""

        app.logger.info(f"Generating {doc_type_name} for {business_name} in {jurisdiction}")

        # --- Call OpenAI API ---
        # Use a reasonable timeout for the API call itself
        openai_timeout = 25 # seconds
        try:
            response = client.chat.completions.create(
                model="gpt-4-turbo", # Consider flexibility via env var: os.getenv("OPENAI_MODEL", "gpt-4-turbo")
                messages=[
                    {"role": "system", "content": "You are an expert legal document generation assistant. Create professional, clear, and legally appropriate documents tailored to user specifications and jurisdiction."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.5, # Adjust creativity vs predictability
                max_tokens=4000, # Max possible for turbo, adjust if needed
                timeout=openai_timeout
            )
            document_text = response.choices[0].message.content
            app.logger.info("Successfully received response from OpenAI.")

        except Exception as e:
             # Catch specific OpenAI errors if needed (e.g., RateLimitError, AuthenticationError)
             app.logger.error(f"OpenAI API call failed: {str(e)}")
             # Re-raise a more generic exception to be caught by the wrapper/caller
             raise Exception(f"OpenAI API Error: {str(e)}") from e


        # --- Generate PDF ---
        unique_id = uuid.uuid4().hex[:8]
        # Sanitize document_type for filename
        safe_doc_type = "".join(c if c.isalnum() else "_" for c in document_type)
        filename = f"{safe_doc_type}_{unique_id}.pdf"
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)

        app.logger.info(f"Generating PDF: {filepath}")
        create_pdf(document_text, filepath, business_name, doc_type_name, jurisdiction)
        app.logger.info(f"PDF created successfully: {filename}")

        return {
            'success': True,
            'download_url': url_for('download_file', filename=filename, _external=False) # Relative URL
        }

    except ValueError as ve:
         # Handle validation errors specifically
         app.logger.error(f"Form validation error during document generation: {str(ve)}")
         raise ve # Re-raise to potentially return 400 Bad Request
    except Exception as e:
        # Catch all other errors during generation (e.g., PDF creation failure)
        app.logger.error(f"Core document generation process failed: {str(e)}")
        # Re-raise the exception to be handled by the calling function (e.g., payment_success)
        # This avoids returning a success=False dict directly from here
        raise Exception(f"Document Generation Failed: {str(e)}") from e


def create_pdf(text, filepath, business_name, document_type, jurisdiction):
    """Creates a PDF document from the generated text using ReportLab."""
    try:
        doc = SimpleDocTemplate(filepath, pagesize=letter,
                              rightMargin=72, leftMargin=72,
                              topMargin=72, bottomMargin=18) # Reduced bottom margin for page num
        styles = getSampleStyleSheet()

        # --- Define Paragraph Styles ---
        title_style = ParagraphStyle(
            'DocTitle', parent=styles['h1'], fontSize=16, alignment=TA_CENTER,
            spaceAfter=6, textColor=colors.HexColor('#1a237e') # Dark blue
        )
        subtitle_style = ParagraphStyle(
             'DocSubtitle', parent=styles['h2'], fontSize=12, alignment=TA_CENTER,
             spaceAfter=20, textColor=colors.darkgrey
        )
        normal_style = ParagraphStyle(
            'BodyText', parent=styles['Normal'], fontSize=10, alignment=TA_JUSTIFY,
            leading=14, spaceBefore=4, spaceAfter=4, firstLineIndent=18
        )
        heading1_style = ParagraphStyle(
             'Heading1', parent=styles['h2'], fontSize=13, alignment=TA_LEFT,
             spaceBefore=12, spaceAfter=6, textColor=colors.HexColor('#1a237e'),
             fontName='Helvetica-Bold', keepWithNext=1 # Keep heading with next paragraph
        )
        heading2_style = ParagraphStyle(
             'Heading2', parent=styles['h3'], fontSize=11, alignment=TA_LEFT,
             spaceBefore=10, spaceAfter=4, textColor=colors.HexColor('#283593'), # Slightly lighter blue
             fontName='Helvetica-Bold', keepWithNext=1
        )
        bullet_style = ParagraphStyle(
             'Bullet', parent=normal_style, firstLineIndent=0, leftIndent=36,
             spaceBefore=2, spaceAfter=2
        )
        signature_style = ParagraphStyle(
            'Signature', parent=styles['Normal'], fontSize=10, alignment=TA_LEFT,
            leading=16, spaceBefore=15, spaceAfter=15, leftIndent=0
        )
        footer_style = ParagraphStyle(
            'Footer', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER,
             textColor=colors.grey
        )

        content = []

        # --- Add Title and Subtitle ---
        content.append(Paragraph(document_type.upper(), title_style))
        content.append(Paragraph(f"For: {business_name}", subtitle_style))
        # content.append(Paragraph(f"Governing Law: {jurisdiction}", subtitle_style)) # Optional
        content.append(Spacer(1, 20))

        # --- Process Document Text ---
        # Simple approach: split by lines, identify potential headings/bullets
        lines = text.splitlines()
        for line in lines:
            stripped_line = line.strip()
            if not stripped_line:
                # Preserve intentional blank lines as small spacers if needed, or skip
                # content.append(Spacer(1, 6))
                continue

            # Basic Heuristic Formatting (Improve if needed based on common LLM output)
            if stripped_line.isupper() and len(stripped_line) > 3 and len(stripped_line) < 60: # Likely Heading 1
                 content.append(Paragraph(stripped_line, heading1_style))
            elif stripped_line.startswith(("**", "# ")): # Markdown H1/Bold or potential H2
                 content.append(Paragraph(stripped_line.replace("**","").replace("# ",""), heading1_style))
            elif stripped_line.startswith(("## ", "***")): # Markdown H2 / H3
                 content.append(Paragraph(stripped_line.replace("## ","").replace("***",""), heading2_style))
            elif stripped_line.startswith(('-', '*', '•', '+')): # Bullets
                 # Add the bullet character manually for consistency if needed: f"• {stripped_line[1:].strip()}"
                 content.append(Paragraph(stripped_line, bullet_style))
            elif "signature:" in stripped_line.lower() or "printed name:" in stripped_line.lower() \
                 or "date:" in stripped_line.lower() or "title:" in stripped_line.lower() \
                 or "____________" in stripped_line: # Signature Lines
                 # Replace multiple underscores with a fixed line for better PDF rendering
                 line_for_pdf = line.replace('______________________', '_' * 30).replace('_______________','_' * 20)
                 content.append(Paragraph(line_for_pdf.replace("\t", "    "), signature_style)) # Preserve spacing, replace tabs
            else: # Normal paragraph
                 content.append(Paragraph(stripped_line, normal_style))

        # --- Build PDF with Page Numbers ---
        def add_page_number(canvas, doc):
             page_num = canvas.getPageNumber()
             text = f"Page {page_num}"
             canvas.saveState()
             canvas.setFont('Helvetica', 8)
             canvas.setFillColor(colors.grey)
             canvas.drawCentredString(letter[0]/2.0, 30, text) # Position near bottom center
             canvas.restoreState()

        doc.build(content, onFirstPage=add_page_number, onLaterPages=add_page_number)

    except Exception as e:
        app.logger.error(f"Failed to create PDF {filepath}: {str(e)}")
        # Re-raise the exception so generate_document knows PDF creation failed
        raise Exception(f"PDF Generation Error: {str(e)}") from e


# --- Download and Static Routes ---
@app.route('/download/<path:filename>') # Use path converter for safety
def download_file(filename):
    """Serve generated documents for download."""
    # Basic security: ensure filename doesn't try to escape the download folder
    if '..' in filename or filename.startswith('/'):
         app.logger.warning(f"Attempted directory traversal in download: {filename}")
         return "Invalid filename", 400

    safe_path = os.path.join(DOWNLOAD_FOLDER, filename)
    # Double check it's still within the intended folder
    if not os.path.normpath(safe_path).startswith(os.path.normpath(DOWNLOAD_FOLDER)):
         app.logger.error(f"Download path escape detected: {filename}")
         return "Invalid path", 400

    if not os.path.exists(safe_path):
        app.logger.warning(f"Download requested for non-existent file: {filename}")
        return "File not found", 404

    app.logger.info(f"Serving file for download: {filename}")
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)


@app.route('/favicon.ico')
def favicon():
    """Serve the favicon."""
    return send_from_directory(STATIC_FOLDER, 'favicon.ico', mimetype='image/vnd.microsoft.icon')


# --- Main Execution ---
if __name__ == '__main__':
    # Use environment variable for port, default to 5000 for local dev
    port = int(os.environ.get('PORT', 5000))
    # Use debug=True only for local development, controlled by env var
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)

# Gunicorn settings comments remain relevant for deployment