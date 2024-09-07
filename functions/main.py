from io import BytesIO
from pydantic import BaseModel
from firebase_functions import https_fn
from firebase_functions import storage_fn
from firebase_functions.firestore_fn import (
  on_document_created,
  Event,
  DocumentSnapshot,
)
from firebase_admin import initialize_app
from firebase_admin import firestore
from firebase_admin import storage as fb_storage
from google.cloud import storage
import firebase_admin
from openai import OpenAI
from firebase_functions.params import SecretParam
import requests
import urllib.parse


OPENAI_API_KEY = SecretParam("OPENAI_API_KEY")
DEFAULT_FRIDGE_ID = "Vely0XkPLzum8Hb5KlTL"

initialize_app()

class RecipeIngredient(BaseModel):
    name: str
    # TODO amount: str
    available: bool

class Recipe(BaseModel):
    name: str
    ingredients: list[RecipeIngredient]
    instructions: str

class InventoryItem(BaseModel):
    name: str
    expiration: str
    open: bool
    type: str

class FridgeInventory(BaseModel):
    date: str
    items: list[InventoryItem]

# For testing
@https_fn.on_request()
def create_empty_shopping_list(request) -> str:
    # Firestore client
    db = firestore.client()
    
    # Create the shopping list in Firestore
    db.collection("fridges").document(DEFAULT_FRIDGE_ID).collection("shopping-lists").add({
        "items": []
    })
    
    return "Empty shopping list created"

# For testing
@https_fn.on_request()
def add_image(request) -> str:
    db = firestore.client()
    db.collection("fridges").document(DEFAULT_FRIDGE_ID).collection("images").add({
        "date": "2024-12-12",
        "urls": ["https://firebasestorage.googleapis.com/v0/b/frost-guardians-83c73.appspot.com/o/mra-blog-how-to-properly-store-a-refrigerator-in-your-garage.webp?alt=media&token=5cc659c9-ee47-489d-b0cb-20b01b2da49d"]
    })
    return "Image added"

# On database trigger in the 'images' collection
@on_document_created(document="fridges/{fridgeId}/images/{imagesId}", secrets=[OPENAI_API_KEY])
def analyze_image(event: Event[DocumentSnapshot]) -> None:
    print("Analyzing image")

    images = event.data.to_dict()
    date = images.get("date")
    urls = images.get("urls")

    image_array = map(lambda url: {"type": "image_url", "image_url": {"url": url}}, urls)

    # Analyze the image
    client = OpenAI()

    completion = client.beta.chat.completions.parse(
        model="gpt-4o-2024-08-06",
        messages=[
            {
                "role": "system",
                "content": "Analyze the image and list the items in the image. Estimate the expiration date of each item. Compare the items to the previous inventory (especially in regard of the open/closed state) and adjust the expiration dates if necessary.",
            },
            # TODO - Add last inventory and date
            {
                "role": "user",
                "content": f"Current date: {date}",
            },
            {
                "role": "user",
                "content": image_array,
            }
        ],
        response_format=FridgeInventory,
    )
    inventory = completion.choices[0].message.parsed
    inventory_data = inventory.dict()

    # For now, just write example data to the database
    db = firestore.client()
    db.collection("fridges").document(event.params["fridgeId"]).collection("inventory").document(event.params["imagesId"]).set(inventory_data)

# On database trigger in the 'inventory' collection
@on_document_created(document="fridges/{fridgeId}/inventory/{inventoryId}", secrets=[OPENAI_API_KEY])
def recommend_recipe(event: Event[DocumentSnapshot]) -> None:
    inventory = event.data.to_dict()

    # Make recipe
    client = OpenAI()
    completion = client.beta.chat.completions.parse(
        model="gpt-4o-2024-08-06",
        messages=[
            {"role": "system", "content": "Make a sensible but creative recipe with some of the given inventory. Try to use the ingredients that are about to expire. If necessary, add some additional ingredients."},
            {"role": "user", "content": f"Inventory: {inventory}"},
        ],
        response_format=Recipe,
    )
    recipe = completion.choices[0].message.parsed
    recipe_data = recipe.dict()

    # Generate Image
    response = client.images.generate(
        model="dall-e-3",
        prompt=f"Photorealistic image of {recipe_data['name']} recipe",
        size="1024x1024",
        quality="standard",
        n=1,
    )
    openai_image_url = response.data[0].url

    # Download image
    response = requests.get(openai_image_url)
    if response.status_code != 200:
        raise Exception(f"Failed to download image from {openai_image_url}, status code: {response.status_code}")

    image_data = BytesIO(response.content)  # Use an in-memory byte stream
    print(f"Image downloaded from {openai_image_url}.")

    # Upload the image data to Google Cloud Storage
    storage_client = storage.Client()
    bucket = storage_client.bucket("frost-recipes-images")
    destination_blob_name = event.params["fridgeId"] + "/recipe-images/" + recipe_data["name"] + ".jpg"
    blob = bucket.blob(destination_blob_name)

    # Upload the image from the in-memory byte stream
    blob.upload_from_file(image_data, content_type=response.headers.get('Content-Type'))
    print(f"Image uploaded to {destination_blob_name}")

    # Store image in Firebase Storage
    bucket = fb_storage.bucket()
    image_url = bucket.blob(destination_blob_name).public_url
    recipe_data["image_url"] = image_url

    # Save recipe to database
    db = firestore.client()
    db.collection("fridges").document(event.params["fridgeId"]).collection("recipes").document(event.params["inventoryId"]).set(recipe_data) # TODO - Allow multiple recipes
    
# On Upload to Firebase Storage create a new document in the 'images' collection
@storage_fn.on_object_finalized(bucket="fridge-captures", region="europe-west3")
def add_image_to_db(event: storage_fn.CloudEvent[storage_fn.StorageObjectData]):
    print("Adding image to database")

    fridge_id, image_id = event.data.name.split("/")

    name = urllib.parse.quote(event.data.name, safe='')
    img_url = f"https://firebasestorage.googleapis.com/v0/b/fridge-captures/o/{name}?alt=media"

    db = firestore.client()
    db.collection("fridges").document(fridge_id).collection("images").document(image_id).set({
        "date": event.data.time_created,
        "urls": [img_url]
    })