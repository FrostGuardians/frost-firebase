from pydantic import BaseModel
from firebase_functions import https_fn
from firebase_functions.firestore_fn import (
  on_document_created,
  Event,
  DocumentSnapshot,
)
from firebase_admin import initialize_app
from firebase_admin import firestore
from openai import OpenAI
from firebase_functions.params import SecretParam

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
def add_image(request):
    db = firestore.client()
    db.collection("fridges").document(DEFAULT_FRIDGE_ID).collection("images").add({
        "date": "2024-12-12",
        "urls": ["https://firebasestorage.googleapis.com/v0/b/frost-guardians-83c73.appspot.com/o/mra-blog-how-to-properly-store-a-refrigerator-in-your-garage.webp?alt=media&token=5cc659c9-ee47-489d-b0cb-20b01b2da49d"]
    })
    return "Image added"

# On database trigger in the 'images' collection
@on_document_created(document="fridges/{fridgeId}/images/{imagesId}", secrets=[OPENAI_API_KEY])
def analyze_image(event: Event[DocumentSnapshot]) -> None:
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
            {"role": "system", "content": "Make a recipe with the given inventory. Try to use the ingredients that are about to expire."},
            {"role": "user", "content": f"Inventory: {inventory}"},
        ],
        response_format=Recipe,
    )
    recipe = completion.choices[0].message.parsed
    recipe_data = recipe.dict()

    # Add Image
    response = client.images.generate(
        model="dall-e-3",
        prompt=f"Photorealistic image of {recipe_data['name']} recipe",
        size="1024x1024",
        quality="standard",
        n=1,
    )
    image_url = response.data[0].url
    recipe_data["image"] = image_url

    # Save recipe to database
    db = firestore.client()
    db.collection("fridges").document(event.params["fridgeId"]).collection("recipes").document(event.params["inventoryId"]).set(recipe_data) # TODO - Allow multiple recipes
    