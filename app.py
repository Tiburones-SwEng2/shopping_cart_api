from flask import Flask, request, jsonify
from flasgger import Swagger
from flask_cors import CORS
from bson import ObjectId
from datetime import datetime
import os
from dotenv import load_dotenv
import requests
from flask_pymongo import PyMongo

# Load environment variables
load_dotenv()

app = Flask(__name__)
swagger = Swagger(app)
CORS(app)

# Configure MongoDB
app.config["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017/shopping_cart_db")
mongo = PyMongo(app)

# Create indexes
mongo.db.cart.create_index([("user_email", 1), ("donation_id", 1)], unique=True)

@app.route('/cart', methods=['POST'])
def add_to_cart():
    """
    Add a donation item to the shopping cart
    ---
    tags:
      - Shopping Cart
    parameters:
      - in: body
        name: cart_item
        required: true
        schema:
          type: object
          required:
            - user_email
            - donation_id
          properties:
            user_email:
              type: string
              example: user@example.com
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
    data = request.get_json()
    
    if not data or 'user_email' not in data or 'donation_id' not in data:
        return jsonify({"error": "Missing required fields"}), 400
    
    # Verify the donation exists and is available
    donation_response = requests.get(f"http://localhost:5000/api/donations/{data['donation_id']}")
    if donation_response.status_code != 200 or not donation_response.json().get('available', False):
        return jsonify({"error": "Donation not available"}), 404
    
    
    cart_item = {
        "id": str(ObjectId()),
        "user_email": data["user_email"],
        "donation_id": data["donation_id"],
        "notes": data.get("notes", ""),
        "created_at": datetime.utcnow().isoformat(),
        "status": "pending"  # pending, claimed, cancelled
    }
    
    try:
        result = mongo.db.cart.insert_one(cart_item)
        cart_item["_id"] = str(result.inserted_id)
        return jsonify(cart_item), 201
    except Exception as e:
        return jsonify({"error": "Item already in cart", "details": str(e)}), 400

@app.route('/cart/<user_email>', methods=['GET'])
def get_cart(user_email):
    """
    Get all items in a user's shopping cart
    ---
    tags:
      - Shopping Cart
    parameters:
      - in: path
        name: user_email
        required: true
        type: string
    responses:
      200:
        description: List of cart items
        schema:
          type: array
          items:
            type: object
            properties:
              id:
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
    """
    user_cart = list(mongo.db.cart.find({"user_email": user_email}))
    
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
                "created_at": item["created_at"].format(),
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
def remove_from_cart(cart_item_id):
    """
    Remove an item from the shopping cart
    ---
    tags:
      - Shopping Cart
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
    """
    try:
        obj_id = ObjectId(cart_item_id)
    except:
        return jsonify({"error": "Invalid cart item ID"}), 400
    
    item = mongo.db.cart.find_one({"_id": obj_id})
    if not item:
        return jsonify({"error": "Item not found in cart"}), 404
    
    result = mongo.db.cart.delete_one({"_id": obj_id})
    
    if result.deleted_count == 1:
        return jsonify({"message": "Item removed from cart"}), 200
    return jsonify({"error": "Item not found in cart"}), 404


@app.route('/cart/<cart_item_id>/claim', methods=['POST'])
def claim_item(cart_item_id):
    """
    Claim a donation item (finalize the request)
    ---
    tags:
      - Shopping Cart
    parameters:
      - in: path
        name: cart_item_id
        required: true
        type: string
    responses:
      200:
        description: Item claimed successfully
      404:
        description: Item not found in cart
      400:
        description: Item already claimed or donation no longer available
    """
    try:
        obj_id = ObjectId(cart_item_id)
    except:
        return jsonify({"error": "Invalid cart item ID"}), 400
    
    item = mongo.db.cart.find_one({"_id": obj_id})
    if not item:
        return jsonify({"error": "Item not found in cart"}), 404
    
    if item['status'] != 'pending':
        return jsonify({"error": "Item already processed"}), 400
    
    # Verify donation is still available
    donation_response = requests.get(f"http://localhost:5000/api/donations/{item['donation_id']}")
    if donation_response.status_code != 200 or not donation_response.json().get('available', False):
        return jsonify({"error": "Donation no longer available"}), 400
    
    # Update donation availability
    update_response = requests.patch(
        f"http://localhost:5000/api/donations/{item['donation_id']}/availability",
        json={"available": "false"}
    )
    
    if update_response.status_code != 200:
        return jsonify({"error": "Could not update donation status"}), 400
    
    # Update cart item status
    mongo.db.cart.update_one(
        {"_id": obj_id},
        {"$set": {
            "status": "claimed",
            "claimed_at": datetime.utcnow()
        }}
    )
    
    # Send notification to donor
    donation_data = donation_response.json()
    notification_data = {
        "email": donation_data["email"],
        "id": donation_data["id"],
        "description": donation_data["description"]
    }
    requests.post("http://localhost:5001/sendNotification", json=notification_data)
    
    # Return updated item
    updated_item = mongo.db.cart.find_one({"_id": obj_id})
    updated_item["_id"] = str(updated_item["_id"])
    updated_item["created_at"] = updated_item["created_at"].format()
    updated_item["claimed_at"] = updated_item["claimed_at"].isoformat()
    
    return jsonify(updated_item), 200

if __name__ == '__main__':
    app.run(debug=True, port=5003)