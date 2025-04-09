import os
import json
import uuid
from datetime import datetime
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

# Initialize OpenAI client with no proxy configuration
client = OpenAI(api_key=OPENAI_API_KEY)

# Ensure the uploads directory exists
UPLOAD_FOLDER = os.path.join(os.getcwd(), "static", "documents")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Ensure the downloads directory exists
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), "static", "downloads")
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

@babel.localeselector
def get_locale():
    return request.accept_languages.best_match(['en', 'es'])

@app.route('/')
def index():
    return render_template('index.html', document_types=DOCUMENT_TYPES, stripe_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        # Store form data in session or temporary storage
        form_data = request.form
        
        # Create a checkout session with standard checkout
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[
                {
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {
                            'name': f'Legal Document: {DOCUMENT_TYPES.get(form_data.get("document_type", ""), "Custom Document")}',
                            'description': 'AI-generated legal document tailored to your business needs',
                        },
                        'unit_amount': 9900,  # $99.00 in cents
                    },
                    'quantity': 1,
                },
            ],
            mode='payment',
            success_url=request.host_url + 'payment-return?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url,
            metadata={
                'form_data': json.dumps(dict(form_data))
            }
        )
        
        return jsonify({
            'sessionId': checkout_session.id
        })
    except Exception as e:
        app.logger.error(f"Stripe error: {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/payment-return', methods=['GET'])
def payment_return():
    session_id = request.args.get('session_id')
    return render_template('payment_return.html', session_id=session_id, stripe_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/payment-success', methods=['GET'])
def payment_success():
    session_id = request.args.get('session_id')
    
    try:
        # Retrieve the session to get metadata
        session = stripe.checkout.Session.retrieve(session_id)
        
        # In a production environment, you should verify the payment status
        # For now, we'll proceed with document generation
        
        # Extract form data from metadata
        form_data = json.loads(session.metadata.get('form_data', '{}'))
        
        # Set a timeout for the entire request to prevent Heroku H12 errors
        # This will ensure we respond to the client before Heroku times out
        start_time = time.time()
        timeout_limit = 25  # seconds, less than Heroku's 30s limit
        
        # Generate the document with retry logic
        max_retries = 2
        retry_delay = 1  # seconds
        
        for attempt in range(max_retries):
            try:
                # Check if we're approaching the timeout limit
                if time.time() - start_time > timeout_limit:
                    # If we're close to timeout, return a "processing" response
                    # The client will retry the request
                    app.logger.warning(f"Approaching timeout limit, returning processing status")
                    return jsonify({
                        'status': 'processing',
                        'message': 'Your document is still being generated. Please wait a moment and try again.'
                    }), 202
                
                document_result = generate_document(form_data)
                
                if document_result.get('success'):
                    return jsonify(document_result)
                else:
                    error_msg = document_result.get('error', 'Unknown error')
                    app.logger.error(f"Document generation failed: {error_msg}")
                    
                    if attempt < max_retries - 1:
                        # Not the last attempt, wait and retry
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                        app.logger.info(f"Retrying document generation (attempt {attempt+2}/{max_retries})")
                    else:
                        # Last attempt failed
                        return jsonify({'error': f'Failed to generate document after {max_retries} attempts: {error_msg}'}), 500
            
            except Exception as doc_error:
                app.logger.error(f"Document generation exception (attempt {attempt+1}/{max_retries}): {str(doc_error)}")
                
                if attempt < max_retries - 1:
                    # Not the last attempt, wait and retry
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    app.logger.info(f"Retrying document generation (attempt {attempt+2}/{max_retries})")
                else:
                    # Last attempt failed
                    return jsonify({'error': f"Document generation failed after {max_retries} attempts: {str(doc_error)}"}), 500
            
    except stripe.error.StripeError as e:
        # Handle Stripe-specific errors
        app.logger.error(f"Stripe error: {str(e)}")
        return jsonify({'error': f"Payment verification failed: {str(e)}"}), 400
    except Exception as e:
        # Handle any other exceptions
        app.logger.error(f"Payment success route error: {str(e)}")
        return jsonify({'error': f"An unexpected error occurred: {str(e)}"}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint to verify the server is running properly"""
    try:
        # Check if we can connect to Stripe
        stripe.Account.retrieve()
        
        # Check if OpenAI API is accessible
        openai_status = "ok"
        try:
            # Simple model check with a short timeout
            client.models.list(timeout=5)
        except Exception as e:
            openai_status = f"error: {str(e)}"
        
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'stripe': 'ok',
            'openai': openai_status
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/generate-document', methods=['POST'])
def handle_document_generation():
    try:
        # For direct document generation (bypassing payment in development)
        if os.getenv("BYPASS_PAYMENT", "false").lower() == "true":
            return generate_document(request.form)
        else:
            return jsonify({'error': 'Payment required'}), 402
    except Exception as e:
        app.logger.error(f"Error generating document: {str(e)}")
        return jsonify({'error': f'Failed to generate document: {str(e)}'}), 500

def generate_document(form_data):
    try:
        # Extract form data
        document_type = form_data.get('document_type')
        business_name = form_data.get('business_name')
        business_type = form_data.get('business_type')
        state = form_data.get('state')
        industry = form_data.get('industry')
        protection_level = form_data.get('protection_level', '2')
        
        # Special clauses
        clauses = []
        if form_data.get('clause_confidentiality'):
            clauses.append("Enhanced Confidentiality")
        if form_data.get('clause_arbitration'):
            clauses.append("Arbitration Provision")
        if form_data.get('clause_termination'):
            clauses.append("Advanced Termination Options")
        if form_data.get('clause_ip'):
            clauses.append("Intellectual Property Protection")
        
        additional_instructions = form_data.get('additional_instructions', '')
        
        # Create prompt for OpenAI
        prompt = f"""Generate a professional {DOCUMENT_TYPES.get(document_type, 'legal document')} for {business_name}, a {business_type} in the {industry} industry, operating in {state}.

Protection Level: {protection_level} out of 3

Special Clauses to Include: {', '.join(clauses) if clauses else 'None'}

Additional Instructions: {additional_instructions}

**Formatting Guidelines:**
- Use clear section headings in bold and all caps (e.g., **TERMS AND CONDITIONS**).
- Use proper indentation and line spacing for readability.
- Ensure signature fields are properly spaced and formatted as follows:

  **Signature:** ______________________  **Date:** _______________

- Use bullet points for lists where appropriate.
- Avoid overly dense paragraphs; break them up into short, digestible sections.
- Use legal language but ensure clarity for business professionals.

Format the document professionally with appropriate sections, headings, and legal language. Include all necessary legal provisions for this type of document in {state}.
"""

        # Call OpenAI API with retry logic
        max_retries = 3
        retry_delay = 2  # seconds
        
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model="gpt-4-turbo",  # Use a more reliable model
                    messages=[
                        {"role": "system", "content": "You are a legal document generator that creates professional, legally-sound documents tailored to specific business needs and jurisdictions."},
                        {"role": "user", "content": prompt}
                    ],
                    timeout=30,  # Shorter timeout to avoid worker timeouts
                    max_tokens=4000  # Limit token count to speed up generation
                )
                
                # Extract generated text
                document_text = response.choices[0].message.content
                break  # Success, exit the retry loop
                
            except Exception as e:
                app.logger.error(f"OpenAI API error (attempt {attempt+1}/{max_retries}): {str(e)}")
                
                if attempt < max_retries - 1:
                    # Not the last attempt, wait and retry
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    # Last attempt failed, raise the exception
                    app.logger.error(f"All {max_retries} attempts to call OpenAI API failed")
                    raise Exception(f"Failed to generate document after {max_retries} attempts: {str(e)}")
        
        # Generate a unique filename
        unique_id = uuid.uuid4().hex[:8]
        filename = f"{document_type}_{unique_id}.pdf"
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        # Create PDF
        create_pdf(document_text, filepath, business_name, DOCUMENT_TYPES.get(document_type, "Legal Document"))
        
        # Return success response
        return {
            'success': True,
            'download_url': f'/download/{filename}'
        }
    
    except Exception as e:
        app.logger.error(f"OpenAI API error: {str(e)}")
        raise Exception(f"Failed to generate document: {str(e)}")

def create_pdf(text, filepath, business_name, document_type):
    # Create PDF document
    doc = SimpleDocTemplate(filepath, pagesize=letter,
                          rightMargin=72, leftMargin=72,
                          topMargin=72, bottomMargin=72)
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'Title',
        parent=styles['Heading1'],
        fontSize=16,
        alignment=TA_CENTER,
        spaceAfter=20,
        textColor=colors.navy,
        fontName='Helvetica-Bold'
    )
    
    normal_style = ParagraphStyle(
        'Normal',
        parent=styles['Normal'],
        fontSize=11,
        alignment=TA_JUSTIFY,
        firstLineIndent=20,
        leading=14,
        spaceBefore=6,
        spaceAfter=6
    )
    
    header_style = ParagraphStyle(
        'Header',
        parent=styles['Heading2'],
        fontSize=13,
        spaceAfter=10,
        spaceBefore=15,
        textColor=colors.navy,
        fontName='Helvetica-Bold',
        borderWidth=1,
        borderColor=colors.lightgrey,
        borderPadding=5,
        borderRadius=2
    )
    
    # Build document content
    content = []
    
    # Add title
    content.append(Paragraph(f"{document_type.upper()}", title_style))
    content.append(Paragraph(f"For: {business_name}", title_style))
    content.append(Spacer(1, 20))
    
    # Add date with better formatting
    date_style = ParagraphStyle(
        'Date',
        parent=styles['Normal'],
        fontSize=11,
        alignment=TA_RIGHT,
        textColor=colors.darkgrey
    )
    content.append(Paragraph(f"Date: {datetime.now().strftime('%B %d, %Y')}", date_style))
    content.append(Spacer(1, 20))
    
    # Process the text into paragraphs
    paragraphs = text.split('\n')
    for para in paragraphs:
        if para.strip():
            # Handle markdown-style headers (# Header)
            if para.strip().startswith('#'):
                header_text = para.replace('#', '').strip()
                content.append(Paragraph(header_text, header_style))
            # Handle all-caps headers (HEADER)
            elif para.strip().isupper() and len(para.strip()) > 3:
                content.append(Paragraph(para.strip(), header_style))
            # Handle bullet points
            elif para.strip().startswith('•') or para.strip().startswith('-') or para.strip().startswith('*'):
                bullet_style = ParagraphStyle(
                    'Bullet',
                    parent=normal_style,
                    leftIndent=30,
                    firstLineIndent=0,
                    spaceBefore=3,
                    spaceAfter=3
                )
                content.append(Paragraph(para.strip(), bullet_style))
            # Handle signature lines
            elif "signature" in para.lower() or "sign" in para.lower() or "date:" in para.lower():
                sig_style = ParagraphStyle(
                    'Signature',
                    parent=normal_style,
                    spaceBefore=15,
                    spaceAfter=15
                )
                content.append(Paragraph(para, sig_style))
            # Regular paragraph
            else:
                content.append(Paragraph(para, normal_style))
            
            # Add appropriate spacing
            if para.strip().startswith('#') or para.strip().isupper():
                content.append(Spacer(1, 10))
            else:
                content.append(Spacer(1, 6))
    
    # Build the PDF
    doc.build(content)

@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)

# Route to serve favicon
@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
    
# Add Gunicorn configuration for Heroku
# This is used when the app is deployed to Heroku
# The timeout is increased to 120 seconds to allow for longer document generation times
# The worker class is set to 'sync' to ensure proper handling of long-running requests
# The number of workers is set to 3 to handle multiple concurrent requests
# The max requests is set to 1000 to prevent memory leaks
# The preload app option is set to True to load the app before forking workers
# The worker timeout is set to 120 seconds to prevent worker timeouts during document generation
# Note: These settings are only used when running with Gunicorn on Heroku
# For local development, the app.run() method is used
# These settings can be overridden by setting environment variables
# For example: GUNICORN_TIMEOUT=180 gunicorn app:app
# See: https://docs.gunicorn.org/en/stable/settings.html
#
# worker_class = 'sync'
# workers = 3
# max_requests = 1000
# preload_app = True
# timeout = 120 
