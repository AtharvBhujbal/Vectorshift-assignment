# slack.py

import json
import secrets
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import base64
import requests
from integrations.integration_item import IntegrationItem
from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

import os 
from dotenv import load_dotenv
load_dotenv()
from hubspot import HubSpot

api_client = HubSpot()
CLIENT_ID = os.getenv('HUBSPOT_CLIENT_ID')
CLIENT_SECRET = os.getenv('HUBSPOT_CLIENT_SECRET')


REDIRECT_URI = 'http://localhost:8000/integrations/hubspot/oauth2callback'
authorization_url = f'https://app.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope=oauth%20crm.objects.companies.read%20crm.schemas.contacts.read%20crm.objects.contacts.read'

async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = json.dumps(state_data)
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', encoded_state, expire=600)
    return f'{authorization_url}&state={encoded_state}'

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error'))
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    state_data = json.loads(encoded_state)

    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')

    saved_state = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')

    if not saved_state or original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='State does not match.')

    async with httpx.AsyncClient() as client:
        response, _ = await asyncio.gather(
            client.post(
                'https://api.hubspot.com/oauth/v1/token',
                data={
                    'grant_type': 'authorization_code',
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                    'redirect_uri': REDIRECT_URI,
                    'code': code
                }
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}')
        )
    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(response.json()),expire=600)

    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='Credentials not found.')
    credentials = json.loads(credentials)
    if not credentials:
        raise HTTPException(status_code=400, detail='Credentials not found.')
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')

    return credentials

def create_integration_item_metadata_object(response_json):
    # print(response_json['id'])
    properties = response_json['properties']
    itegration_item_metadata = IntegrationItem(
        id=response_json['id'],
        name=" ".join([properties['firstname'], properties['lastname']]),
        creation_time=properties['createdate'],
        last_modified_time=properties['lastmodifieddate']
    )
    return itegration_item_metadata

async def get_items_hubspot(credentials):
    """Aggregates all metadata relevant for a HubSpot integration"""
    credentials = json.loads(credentials)
    response = requests.get(
        'https://api.hubapi.com/crm/v3/objects/contacts',
        headers={
            'Authorization': f'Bearer {credentials.get("access_token")}',
            'Content-Type': 'application/json'
        },
    )

    list_of_integration_item_metadata = []
    if response.status_code == 200:
        results = response.json().get('results', [])
        for result in results:
            list_of_integration_item_metadata.append(
                create_integration_item_metadata_object(result)
            )

    return list_of_integration_item_metadata