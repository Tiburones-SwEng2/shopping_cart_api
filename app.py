from flask import Flask, request, jsonify
from flasgger import Swagger
from flask_cors import CORS
from flask_pymongo import PyMongo
from bson import ObjectId
from datetime import datetime
from datetime import timedelta
import os
from dotenv import load_dotenv
import requests
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity
from prometheus_client import Counter, Histogram, generate_latest
import time
from functools import wraps

# Load environment variables
load_dotenv()

app = Flask(__name__)
swagger = Swagger(app)
CORS(app)

# MÉTRICAS
REQUEST_COUNT = Counter('shopping_cart_http_requests_total', 'Total Requests', ['method', 'endpoint'])
REQUEST_LATENCY = Histogram('shopping_cart_http_request_duration_seconds', 'Request Latency', ['endpoint'])
ERROR_COUNT = Counter('shopping_cart_http_request_errors_total', 'Total Errors', ['endpoint'])

def monitor_metrics(f):
    """Decorador para monitorear métricas de Prometheus"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        endpoint = request.endpoint or 'unknown'
        method = request.method
        
        # Incrementar contador de requests
        REQUEST_COUNT.labels(method=method, endpoint=endpoint).inc()
        
        try:
            # Ejecutar la función
            response = f(*args, **kwargs)
            return response
        except Exception as e:
            # Incrementar contador de errores
            ERROR_COUNT.labels(endpoint=endpoint).inc()
            raise
        finally:
            # Medir latencia
            duration = time.time() - start_time
            REQUEST_LATENCY.labels(endpoint=endpoint).observe(duration)
    
    return decorated_function

# Configure MongoDB
app.config["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017/shopping_cart_db")
mongo = PyMongo(app)

# Configure JWT
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "lkhjap8gy2p 03kt")
jwt = JWTManager(app)
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=1)  # o más

# Create indexes
mongo.db.cart.create_index([("user_email", 1), ("donation_id", 1)], unique=True)

@app.route('/cart', methods=['POST'])
@monitor_metrics
@jwt_required()
def add_to_cart():
    """
    Add a donation item to the shopping cart
    ---
    tags:
      - Shopping Cart
    security:
      - JWT: []
    parameters:
      - in: body
        name: cart_item
        required: true
        schema:
          type: object
          required:
            - donation_id
          properties:
            donation_id:
              type: string
              example: 64a89f1234abcdef5678abcd
            notes:
              type: string
              example: I can pick it up on weekends
    responses:
      201:
        description: Item added to cart successfully
      400:
        description: Missing required fields
      404:
        description: Donation not found or not available
    """
    current_user = get_jwt_identity()
    data = request.get_json()
    
    if not data or 'donation_id' not in data:
        return jsonify({"error": "Missing required fields"}), 400
    
    # Verify the donation exists and is available
    donation_response = requests.get(f"http://localhost:5000/api/donations/{data['donation_id']}")
    if donation_response.status_code != 200 or not donation_response.json().get('available', False):
        return jsonify({"error": "Donation not available"}), 404
    
    cart_item = {
        "user_email": current_user,
        "donation_id": data["donation_id"],
        "notes": data.get("notes", ""),
        "created_at": datetime.utcnow(),
        "status": "pending"  # pending, claimed, cancelled
    }
    
    try:
        result = mongo.db.cart.insert_one(cart_item)
        cart_item["_id"] = str(result.inserted_id)
        return jsonify(cart_item), 201
    except Exception as e:
        return jsonify({"error": "Item already in cart", "details": str(e)}), 400

@app.route('/cart', methods=['GET'])
@monitor_metrics
@jwt_required()
def get_cart():
    """
    Get all items in a user's shopping cart
    ---
    tags:
      - Shopping Cart
    security:
      - JWT: []
    responses:
      200:
        description: List of cart items
        schema:
          type: array
          items:
            type: object
            properties:
              _id:
                type: string
              user_email:
                type: string
              donation_id:
                type: string
              notes:
                type: string
              created_at:
                type: string
              status:
                type: string
              donation_details:
                type: object
    """
    current_user = get_jwt_identity()
    user_cart = list(mongo.db.cart.find({"user_email": current_user}))
    
    # Enhance with donation details
    enhanced_cart = []
    for item in user_cart:
        donation_response = requests.get(f"http://localhost:5000/api/donations/{item['donation_id']}")
        if donation_response.status_code == 200:
            donation_data = donation_response.json()
            enhanced_item = {
                "_id": str(item["_id"]),
                "user_email": item["user_email"],
                "donation_id": item["donation_id"],
                "notes": item.get("notes", ""),
                "created_at": item["created_at"].isoformat(),
                "status": item["status"],
                "donation_details": {
                    "title": donation_data.get("title"),
                    "description": donation_data.get("description"),
                    "category": donation_data.get("category"),
                    "condition": donation_data.get("condition"),
                    "image_url": donation_data.get("image_url"),
                    "city": donation_data.get("city")
                }
            }
            enhanced_cart.append(enhanced_item)
    
    return jsonify(enhanced_cart), 200

@app.route('/cart/<cart_item_id>', methods=['DELETE'])
@monitor_metrics
@jwt_required()
def remove_from_cart(cart_item_id):
    """
    Remove an item from the shopping cart
    ---
    tags:
      - Shopping Cart
    security:
      - JWT: []
    parameters:
      - in: path
        name: cart_item_id
        required: true
        type: string
    responses:
      200:
        description: Item removed successfully
      404:
        description: Item not found in cart
      403:
        description: Not authorized to remove this item
    """
    current_user = get_jwt_identity()
    
    try:
        obj_id = ObjectId(cart_item_id)
    except:
        return jsonify({"error": "Invalid cart item ID"}), 400
    
    item = mongo.db.cart.find_one({"_id": obj_id})
    if not item:
        return jsonify({"error": "Item not found in cart"}), 404
    
    if item["user_email"] != current_user:
        return jsonify({"error": "Not authorized to remove this item"}), 403
    
    result = mongo.db.cart.delete_one({"_id": obj_id})
    
    if result.deleted_count == 1:
        return jsonify({"message": "Item removed from cart"}), 200
    return jsonify({"error": "Item not found in cart"}), 404

@app.route('/cart/<cart_item_id>/claim', methods=['POST'])
@monitor_metrics
@jwt_required()
def claim_item(cart_item_id):
    """
    Claim a donation item (finalize the request)
    ---
    tags:
      - Shopping Cart
    security:
      - JWT: []
    parameters:
      - in: path
        name: cart_item_id
        required: true
        type: string
    responses:
      200:
        description: Item claimed successfully or already claimed
        schema:
          type: object
          properties:
            message:
              type: string
            item_id:
              type: string
            status:
              type: string
            donation_id:
              type: string
      400:
        description: Invalid request or item cannot be claimed
      404:
        description: Item not found in cart
      403:
        description: Not authorized to claim this item
    """
    current_user = get_jwt_identity()
    
    try:
        obj_id = ObjectId(cart_item_id)
    except:
        return jsonify({"error": "Invalid cart item ID"}), 400
    
    # Find the cart item and verify ownership
    item = mongo.db.cart.find_one({"_id": obj_id})
    if not item:
        return jsonify({"error": "Item not found in cart"}), 404
    
    if item["user_email"] != current_user:
        return jsonify({"error": "Not authorized to claim this item"}), 403
    
    # If already claimed, return success
    if item['status'] == 'claimed':
        return jsonify({
            "message": "Item already claimed",
            "item_id": cart_item_id,
            "status": "claimed",
            "donation_id": str(item["donation_id"])
        }), 200
    
    if item['status'] != 'pending':
        return jsonify({
            "error": "Item already processed",
            "current_status": item['status']
        }), 400
    
    # Verify donation is still available
    try:
        donation_response = requests.get(
            f"http://localhost:5000/api/donations/{item['donation_id']}",
            timeout=5
        )
        
        if donation_response.status_code != 200:
            return jsonify({
                "error": "Donation verification failed",
                "details": f"Status code: {donation_response.status_code}"
            }), 400
            
        donation_data = donation_response.json()
        
        if not donation_data.get('available', False):
            return jsonify({
                "error": "Donation no longer available",
                "donation_id": str(item["donation_id"])
            }), 400
            
    except requests.exceptions.RequestException as e:
        return jsonify({
            "error": "Donation service unavailable",
            "details": str(e)
        }), 503
    
    # Update donation availability
    try:
        auth_header = request.headers.get('Authorization')  # asegúrate de obtenerlo antes si no está
        update_response = requests.patch(
        f"http://localhost:5000/api/donations/{item['donation_id']}/availability",
        json={"available": False},
        headers={"Authorization": auth_header},  # añade el JWT aquí
        timeout=5
        )

        
        if update_response.status_code != 200:
            return jsonify({
                "error": "Could not update donation status",
                "details": update_response.json()
            }), 400
            
    except requests.exceptions.RequestException as e:
        return jsonify({
            "error": "Failed to update donation availability",
            "details": str(e)
        }), 503
    
    # Update cart item status
    try:
        update_result = mongo.db.cart.update_one(
            {"_id": obj_id},
            {"$set": {
                "status": "claimed",
                "claimed_at": datetime.utcnow()
            }}
        )
        
        if update_result.modified_count != 1:
            return jsonify({
                "error": "Failed to update cart item status",
                "details": "No documents modified"
            }), 500
            
    except Exception as e:
        return jsonify({
            "error": "Database update failed",
            "details": str(e)
        }), 500
    
    # Send notification to donor
    try:
        notification_data = {
            "email": donation_data["email"], 
            "id": str(item["donation_id"]),
            "description": donation_data["description"],
            "title": donation_data.get("title", "Sin título"),
            "claimer_email": current_user
        }

        auth_header = request.headers.get('Authorization')
        notification_response = requests.post(
            "http://localhost:5001/sendNotification",
            json=notification_data,
            headers={"Authorization": auth_header},
            timeout=10
        )
        
        if notification_response.status_code != 200:
            # No fallar la operación principal si solo falla la notificación
            print(f"Notification failed: {notification_response.text}")
            
    except Exception as e:
        print(f"Notification error: {str(e)}")
    
    # Return success with detailed information
    return jsonify({
        "message": "Item claimed successfully",
        "item_id": cart_item_id,
        "donation_id": str(item["donation_id"]),
        "status": "claimed",
        #"claimed_at": datetime.utcnow().isoformat(),
        "donation_details": {
            "title": donation_data.get("title"),
            "category": donation_data.get("category")
        }
    }), 200

@app.route('/cart/clear-all', methods=['DELETE'])
@monitor_metrics
@jwt_required()
def clear_all_cart():
    """
    Clear ALL items from the shopping cart (regardless of status)
    ---
    tags:
      - Shopping Cart
    security:
      - JWT: []
    responses:
      200:
        description: Cart cleared successfully
        schema:
          type: object
          properties:
            message:
              type: string
            deleted_count:
              type: integer
      404:
        description: No items found to clear
    """
    current_user = get_jwt_identity()
    
    result = mongo.db.cart.delete_many({
        "user_email": current_user  # Elimina todos sin filtrar por status
    })
    
    if result.deleted_count > 0:
        return jsonify({
            "message": "Cart completely cleared",
            "deleted_count": result.deleted_count
        }), 200
    return jsonify({"message": "No items in cart"}), 404

@app.route("/metrics", methods=["GET"])
def metrics():
    """
    Endpoint para exponer métricas de Prometheus
    ---
    tags:
      - Métricas
    responses:
      200:
        description: Métricas de Prometheus
        content:
          text/plain:
            schema:
              type: string
    """
    return generate_latest(), 200, {'Content-Type': 'text/plain; charset=utf-8'}

if __name__ == '__main__':
    app.run(debug=True, port=5003)