Get started with the Quickstart 
================================

#### A quick introduction to building with Plaid 

[Watch video](https://www.youtube.com/watch?v=U9xa1gzyPx8)

Want more video tutorials? The [full getting started guide](https://www.youtube.com/watch?v=sGBvKDGgPjc) for the Quickstart app is available on YouTube.

Don't want to write code? Check out the [Plaid Postman Collection](https://github.com/plaid/plaid-postman) for a no-code way to get started with Plaid's API.

Looking to get started with Identity Verification, Income Verification, Plaid Check CRA, or Layer?

For Identity Verification, check out the [Identity Verification Quickstart](https://github.com/plaid/idv-quickstart) .

For Plaid Check (CRA), check out the [Plaid Credit (CRA) Quickstart](https://github.com/plaid/credit-quickstart) .

For Income, try the [Income Verification Starter app](https://github.com/plaid/income-sample) .

For Layer, use the [Layer Quickstart](https://github.com/plaid/layer-quickstart) .

### Introduction 

These instructions are also available on GitHub at the [Quickstart Repo](https://github.com/plaid/quickstart) .

Let's test out running Plaid locally by cloning the Quickstart app. You'll need API keys, which you can receive by signing up in the [Dashboard](https://dashboard.plaid.com/developers/keys) .

You'll have two different API keys, and there are three different Plaid environments. Today we'll start in the Sandbox environment. View the API Keys section of the Dashboard to find your Sandbox secret.

###### API Key 

[View Keys in Dashboard](https://dashboard.plaid.com/developers/keys)

client\_id

Private identifier for your team

secret

Private key, one for each of the three environments

###### Environment 

Sandbox

Get started with test credentials and life-like data

Production

Test or launch your app with unlimited live credentials

If you get stuck at any point in the Quickstart, help is just a click away! Check the Quickstart [troubleshooting guide](https://github.com/plaid/quickstart#troubleshooting) or ask other developers in our [Discord](https://discord.gg/sf57M8DW3y) or [Stack Overflow community](https://stackoverflow.com/questions/tagged/plaid) .

### Quickstart setup 

Once you have your API keys, it's time to run the Plaid Quickstart locally! The instructions below will guide you through the process of cloning the [Quickstart repository](https://github.com/plaid/quickstart) , customizing the .env file with your own Plaid client ID and Sandbox secret, and finally, building and running the app.

Make sure you have [npm installed](https://docs.npmjs.com/downloading-and-installing-node-js-and-npm) before following along. If you're using Windows, ensure you have a terminal capable of running basic Unix shell commands.

```node
# Note: If on Windows, run
# git clone -c core.symlinks=true https://github.com/plaid/quickstart
# instead to ensure correct symlink behavior

git clone https://github.com/plaid/quickstart.git

# Copy the .env.example file to .env, then fill
# out PLAID_CLIENT_ID and PLAID_SECRET in .env
cp .env.example .env

cd quickstart/node

# Install dependencies
npm install

# Start the backend app
./start.sh
```

```bash
# not applicable for curl, pick a different language from the dropdown
```

```ruby
# Note: If on Windows, run
# git clone -c core.symlinks=true https://github.com/plaid/quickstart
# instead to ensure correct symlink behavior

git clone https://github.com/plaid/quickstart.git

# Copy the .env.example file to .env, then fill
# out PLAID_CLIENT_ID and PLAID_SECRET in .env
cp .env.example .env

cd quickstart/ruby

# Install dependencies
bundle

# Start the backend app
./start.sh
```

```python
# Note: If on Windows, run
# git clone -c core.symlinks=true https://github.com/plaid/quickstart
# instead to ensure correct symlink behavior

git clone https://github.com/plaid/quickstart.git

# Copy the .env.example file to .env, then fill
# out PLAID_CLIENT_ID and PLAID_SECRET in .env
cp .env.example .env

cd quickstart/python

# Note: must use python 3

# For virtualenv users:
# virtualenv venv
# source venv/bin/activate
pip3 install -r requirements.txt

# Start the backend app
./start.sh
```

```java
# Note: If on Windows, run
# git clone -c core.symlinks=true https://github.com/plaid/quickstart
# instead to ensure correct symlink behavior

git clone https://github.com/plaid/quickstart.git

# Copy the .env.example file to .env, then fill
# out PLAID_CLIENT_ID and PLAID_SECRET in .env
cp .env.example .env

cd quickstart/java

mvn clean package

# Start the backend app
./start.sh
```

```go
# Note: If on Windows, run
# git clone -c core.symlinks=true https://github.com/plaid/quickstart
# instead to ensure correct symlink behavior

git clone https://github.com/plaid/quickstart.git

# Copy the .env.example file to .env, then fill
# out PLAID_CLIENT_ID and PLAID_SECRET in .env
cp .env.example .env

cd quickstart/go

go build

# Start the backend app
./start.sh
```

Open a new shell and start the frontend app. Your app will be running at `http://localhost:3000`.

Run the Quickstart frontend

```bash
# Install dependencies
cd quickstart/frontend
npm install

# Start the frontend app
npm start

# Go to http://localhost:3000
```

Visit localhost and log in with Sandbox credentials (typically `user_good` and `pass_good`, as indicated at the bottom of the page).

(An image of "Plaid Quickstart guide with 'Launch Link' button to simulate user bank account integration.")

#### Create your first Item 

Most API requests interact with an _Item_, which is a Plaid term for a login at a financial institution. A single end-user of your application might have accounts at different financial institutions, which means they would have multiple different Items. An Item is not the same as a financial institution account, although every account will be associated with an Item. For example, if a user has one login at their bank that allows them to access both their checking account and their savings account, a single Item would be associated with both of those accounts.

Now that you have the Quickstart running, you'll add your first Item in the Sandbox environment. Once you've opened the Quickstart app on localhost, click the **Launch Link** button and select any institution. Use the Sandbox credentials to simulate a successful login.

##### Sandbox credentials 

```bash
username: user_good
password: pass_good
If prompted to enter a 2FA code: 1234
```

Once you have entered your credentials and moved to the next screen, you have created your first Item! You can now make API calls for that Item by using the buttons in the Quickstart. In the next section, we'll explain what actually happened and how the Quickstart works.

### How it works 

As you might have noticed, you use both a server and a client-side component to access the Plaid APIs. The flow looks like this:

**The Plaid flow** begins when your user wants to connect their bank account to your app.

(An image of "Step diagram")

**1**Call [/link/token/create](https://plaid.com/docs/api/link/index.html.md#linktokencreate) to create a `link_token` and pass the temporary token to your app's client.

(An image of "Step 1 diagram")

**2**Use the `link_token` to open Link for your user. In the [onSuccess callback](https://plaid.com/docs/link/web/index.html.md#onsuccess) , Link will provide a temporary `public_token`. This token can also be obtained on the backend via `/link/token/get`.

(An image of "Step 2 diagram")

**3**Call [/item/public\_token/exchange](https://plaid.com/docs/api/items/index.html.md#itempublic_tokenexchange) to exchange the `public_token` for a permanent `access_token` and `item_id` for the new `Item`.

(An image of "Step 3 diagram")

**4**Store the `access_token` and use it to make product requests for your user's `Item`.

(An image of "Step 4 diagram")

The first step is to create a new `link_token` by making a [/link/token/create](https://plaid.com/docs/api/link/index.html.md#linktokencreate) request and passing in the required configurations. This `link_token` is a short lived, one-time use token that authenticates your app with [Plaid Link](https://plaid.com/docs/link/index.html.md) , our frontend module. Several of the environment variables you configured when launching the Quickstart, such as `PLAID_PRODUCTS`, are used as parameters for the `link_token`.

```node
app.post('/api/create_link_token', async function (request, response) {
  // Get the client_user_id by searching for the current user
  const user = await User.find(...);
  const clientUserId = user.id;
  const linkTokenRequest = {
    user: {
      // This should correspond to a unique id for the current user.
      client_user_id: clientUserId,
    },
    client_name: 'Plaid Test App',
    products: ['auth'],
    language: 'en',
    webhook: 'https://webhook.example.com',
    redirect_uri: 'https://domainname.com/oauth-page.html',
    country_codes: ['US'],
  };
  try {
    const createTokenResponse = await client.linkTokenCreate(linkTokenRequest);
    response.json(createTokenResponse.data);
  } catch (error) {
    // handle error
  }
});

```

```bash
curl -X POST https://sandbox.plaid.com/link/token/create \
-H 'Content-Type: application/json' \
-d '{
  "client_id": "${PLAID_CLIENT_ID}",
  "secret": "${PLAID_SECRET}",
  "client_name": "Plaid Test App",
  "user": { "client_user_id": "${UNIQUE_USER_ID}" },
  "products": ["auth"],
  "country_codes": ["US"],
  "language": "en",
  "webhook": "https://webhook.example.com",
  "redirect_uri": "https://domainname.com/oauth-page.html"
}'

```

```ruby
post '/api/create_link_token' do
  # Get the client_user_id by searching for the current user
  current_user = User.find(...)
  client_user_id = current_user.id

  # Create a link_token for the given user
  request = Plaid::LinkTokenCreateRequest.new(
    {
      user: { client_user_id: client_user_id },
      client_name: 'Plaid Test App',
      products: ['auth'],
      country_codes: ['US'],
      language: "en",
      redirect_uri: nil_if_empty_envvar('PLAID_REDIRECT_URI'),
      webhook: 'https://webhook.example.com'
    }
  )
  response = client.link_token_create(request)
  content_type :json
  response.to_json
end

```

```java
import com.plaid.client.model.Products;
import com.plaid.client.model.CountryCode;
import com.plaid.client.model.LinkTokenCreateRequest;
import com.plaid.client.model.LinkTokenCreateRequestUser;
import com.plaid.client.model.LinkTokenCreateResponse;

public class PlaidExample {

  ...
  static class GetLinkToken implements HttpHandler {
    private static PlaidApi plaidClient;

    public void handle(HttpExchange t) throws IOException {
      // Create your Plaid client
      HashMap apiKeys = new HashMap();
      apiKeys.put("clientId", CLIENT_ID);
      apiKeys.put("secret", SECRET);
      ApiClient apiClient = new ApiClient(apiKeys);
      apiClient.setPlaidAdapter(ApiClient.Sandbox);

      plaidClient = apiClient.createService(PlaidApi.class);

      // Get the clientUserId by searching for the current user
      User userFromDB = db.find(...);
      String clientUserId = userFromDB.id;
      LinkTokenCreateRequestUser user = new LinkTokenCreateRequestUser()
        .clientUserId(clientUserId);

      // Create a link_token for the given user
      LinkTokenCreateRequest request = new LinkTokenCreateRequest()
        .user(user)
        .clientName("Plaid Test App")
        .products(Arrays.asList(Products.fromValue("auth")))
        .countryCodes(Arrays.asList(CountryCode.US))
        .language("en")
        .redirectUri("https://domainname.com/oauth-page.html")
        .webhook("https://webhook.example.com");

      Response response = plaidClient
        .linkTokenCreate(request)
        .execute();

      // Send the data to the client
      return response.body();
    }
  }
}

```

```python
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.country_code import CountryCode

@app.route("/create_link_token", methods=['POST'])
def create_link_token():
    # Get the client_user_id by searching for the current user
    user = User.find(...)
    client_user_id = user.id

    # Create a link_token for the given user
    request = LinkTokenCreateRequest(
            products=[Products("auth")],
            client_name="Plaid Test App",
            country_codes=[CountryCode('US')],
            redirect_uri='https://domainname.com/oauth-page.html',
            language='en',
            webhook='https://webhook.example.com',
            user=LinkTokenCreateRequestUser(
                client_user_id=client_user_id
            )
        )
    response = client.link_token_create(request)

    # Send the data to the client
    return jsonify(response.to_dict())


```

```go
func createLinkToken(c *gin.Context) {
  ctx := context.Background()

  // Get the client_user_id by searching for the current user
  user, _ := usermodels.Find(...)
  clientUserId := user.ID.String()

  // Create a link_token for the given user
  request := plaid.NewLinkTokenCreateRequest("Plaid Test App", "en", []plaid.CountryCode{plaid.COUNTRYCODE_US}, *plaid.NewLinkTokenCreateRequestUser(clientUserId))
  request.SetWebhook("https://webhook.sample.com")
  request.SetRedirectUri("https://domainname.com/oauth-page.html")
  request.SetProducts([]plaid.Products{plaid.PRODUCTS_AUTH})

  resp, _, err := testClient.PlaidApi.LinkTokenCreate(ctx).LinkTokenCreateRequest(*request).Execute()

  // Send the data to the client
  c.JSON(http.StatusOK, gin.H{
    "link_token": resp.GetLinkToken(),
  })
}


```

Once you have a `link_token`, you can use it to initialize [Link](https://plaid.com/docs/link/index.html.md) . Link is a drop-in client-side module available for web, iOS, and Android that handles the authentication process. The Quickstart uses Plaid's optional React bindings for an integration that you trigger via your own client-side code. This is what your users use to log into their financial institution accounts.

After a user submits their credentials within Link, Link provides you with a `public_token` via the `onSuccess` callback. The code below shows how the Quickstart passes the `public_token` from client-side code to the server. Both React and vanilla JavaScript examples are shown.

```javascript
Link Account



(async function($) {
  var handler = Plaid.create({
    // Create a new link_token to initialize Link
    token: (await $.post('/create_link_token')).link_token,
    onLoad: function() {
      // Optional, called when Link loads
    },
    onSuccess: function(public_token, metadata) {
      // Send the public_token to your app server.
      // The metadata object contains info about the institution the
      // user selected and the account ID or IDs, if the
      // Account Select view is enabled.
      $.post('/exchange_public_token', {
        public_token: public_token,
      });
    },
    onExit: function(err, metadata) {
      // The user exited the Link flow.
      if (err != null) {
        // The user encountered a Plaid API error prior to exiting.
      }
      // metadata contains information about the institution
      // that the user selected and the most recent API request IDs.
      // Storing this information can be helpful for support.
    },
    onEvent: function(eventName, metadata) {
      // Optionally capture Link flow events, streamed through
      // this callback as your users connect an Item to Plaid.
      // For example:
      // eventName = "TRANSITION_VIEW"
      // metadata  = {
      //   link_session_id: "123-abc",
      //   mfa_type:        "questions",
      //   timestamp:       "2017-09-14T14:42:19.350Z",
      //   view_name:       "MFA",
      // }
    }
  });

  $('#link-button').on('click', function(e) {
    handler.open();
  });
})(jQuery);


```

```tsx
// APP COMPONENT
// Upon rendering of App component, make a request to create and
// obtain a link token to be used in the Link component
import React, { useEffect, useState } from 'react';
import { usePlaidLink } from 'react-plaid-link';
const App = () => {
  const [linkToken, setLinkToken] = useState(null);
  const generateToken = async () => {
    const response = await fetch('/api/create_link_token', {
      method: 'POST',
    });
    const data = await response.json();
    setLinkToken(data.link_token);
  };
  useEffect(() => {
    generateToken();
  }, []);
  return linkToken != null ?  : <>;
};
// LINK COMPONENT
// Use Plaid Link and pass link token and onSuccess function
// in configuration to initialize Plaid Link
interface LinkProps {
  linkToken: string | null;
}
const Link: React.FC = (props: LinkProps) => {
  const onSuccess = React.useCallback(async (public_token, metadata) => {
    // send public_token to server
    const response = await fetch('/api/set_access_token', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ public_token }),
    });
    // Handle response ...
  }, []);
  const config: Parameters[0] = {
    token: props.linkToken!,
    onSuccess,
  };
  const { open, ready } = usePlaidLink(config);
  return (
     open()} disabled={!ready}>
      Link account
    
  );
};
export default App;

```

Next, on the server side, the Quickstart calls [/item/public\_token/exchange](https://plaid.com/docs/api/items/index.html.md#itempublic_tokenexchange) to obtain an `access_token`, as illustrated in the code excerpt below. The `access_token` uniquely identifies an Item and is a required argument for most Plaid API endpoints. In your own code, you'll need to securely store your `access_token` in order to make API requests for that Item.

```node
app.post('/api/exchange_public_token', async function (
  request,
  response,
  next,
) {
  const publicToken = request.body.public_token;
  try {
    const tokenResponse = await client.itemPublicTokenExchange({
      public_token: publicToken,
    });

    // These values should be saved to a persistent database and
    // associated with the currently signed-in user
    const accessToken = tokenResponse.data.access_token;
    const itemID = tokenResponse.data.item_id;

    response.json({ public_token_exchange: 'complete' });
  } catch (error) {
    // handle error
  }
});

```

```bash
curl -X POST https://sandbox.plaid.com/item/public_token/exchange \
-H 'Content-Type: application/json' \
-d '{
  "client_id": "${PLAID_CLIENT_ID}",
  "secret": "${PLAID_SECRET}",
  "public_token": "public-sandbox-12345678-abcd-1234-abcd-1234567890ab"
}'

```

```ruby
access_token = nil

post '/api/exchange_public_token' do
  request = Plaid::ItemPublicTokenExchangeRequest.new(
    {
      public_token: params["public_token"]
    }
  )
  response = client.item_public_token_exchange(request)

  # These values should be saved to a persistent database and
  # associated with the currently signed-in user
  access_token = response.access_token
  item_id = response.item_id

  content_type :json
  {public_token_exchange: "complete"}.to_json
end

```

```java
import com.plaid.client.model.ItemPublicTokenExchangeRequest;
import com.plaid.client.model.ItemPublicTokenExchangeResponse;

public class PlaidExample {

  ...
  static class GetAccessToken implements HttpHandler {
    private static PlaidClient plaidClient;

    private String publicToken;
    private String accessToken;
    private String itemId;

    public void handle(HttpExchange t) throws IOException {
      // Simplified pseudo-code for obtaining public_token
      InputStream is = t.getRequestBody();
      publicToken = is.publicToken();

      // Create your Plaid client
      HashMap apiKeys = new HashMap();
      apiKeys.put("clientId", CLIENT_ID);
      apiKeys.put("secret", SECRET);
      apiKeys.put("plaidVersion", "2020-09-14");
      apiClient = new ApiClient(apiKeys);
      apiClient.setPlaidAdapter(ApiClient.Sandbox);

      plaidClient = apiClient.createService(PlaidApi.class);

      // Exchange public_token for an access_token
      ItemPublicTokenExchangeRequest request = new ItemPublicTokenExchangeRequest()
        .publicToken(publicToken);

      Response response = plaidClient
        .itemPublicTokenExchange(request)
        .execute();

      // These values should be saved to a persistent database and
      // associated with the currently signed-in user
      accessToken = response.body().getAccessToken();
      itemId      = response.body().getItemId();

      String message = "{\"public_token_exchange\": \"complete\"}";
      return Response
        .status(Response.Status.OK)
        .entity(message)
        .type(MediaType.APPLICATION_JSON)
      }
  }
}

```

```python
access_token = None
item_id = None

@app.route('/exchange_public_token', methods=['POST'])
def exchange_public_token():
    global access_token
    public_token = request.form['public_token']
    request = ItemPublicTokenExchangeRequest(
      public_token=public_token
    )
    response = client.item_public_token_exchange(request)

    # These values should be saved to a persistent database and
    # associated with the currently signed-in user
    access_token = response['access_token']
    item_id = response['item_id']

    return jsonify({'public_token_exchange': 'complete'})

```

```go
func getAccessToken(c *gin.Context) {
  ctx := context.Background()
  publicToken := c.PostForm("public_token")

  // exchange the public_token for an access_token
  exchangePublicTokenReq := plaid.NewItemPublicTokenExchangeRequest(publicToken)
    exchangePublicTokenResp, _, err := client.PlaidApi.ItemPublicTokenExchange(ctx).ItemPublicTokenExchangeRequest(
        *exchangePublicTokenReq,
    ).Execute()

  // These values should be saved to a persistent database and
  // associated with the currently signed-in user
  accessToken := exchangePublicTokenResp.GetAccessToken()
  itemID := exchangePublicTokenResp.GetItemId()

  c.JSON(http.StatusOK, gin.H{"public_token_exchange": "complete"})
}



```

#### Making API requests 

Now that we've gone over the Link flow and token exchange process, we can explore what happens when you press a button in the Quickstart to make an API call. As an example, we'll look at the Quickstart's call to [/accounts/get](https://plaid.com/docs/api/accounts/index.html.md#accountsget) , which retrieves basic information, such as name and balance, about the accounts associated with an Item. The call is fairly straightforward and uses the `access_token` as a single argument to the Plaid client object.

```python
# Retrieve an Item's accounts
@app.route('/accounts', methods=['GET'])
def get_accounts():
  try:
      request = AccountsGetRequest(
          access_token=access_token
      )
      accounts_response = client.accounts_get(request)
  except plaid.ApiException as e:
      response = json.loads(e.body)
      return jsonify({'error': {'status_code': e.status, 'display_message':
                      response['error_message'], 'error_code': response['error_code'], 'error_type': response['error_type']}})
  return jsonify(accounts_response.to_dict())

```

```bash
curl -X POST https://sandbox.plaid.com/accounts/get \
  -H 'Content-Type: application/json' \
  -d '{
    "client_id": "${PLAID_CLIENT_ID}",
    "secret": "${PLAID_SECRET}",
    "access_token": String
  }'

```

```ruby
# Retrieve an Item's accounts
get '/accounts' do
  begin
    product_response = client.accounts.get(access_token)
    pretty_print_response(product_response)
    content_type :json
    { accounts: product_response }.to_json
  rescue Plaid::PlaidAPIError => e
    error_response = format_error(e)
    pretty_print_response(error_response)
    content_type :json
    error_response.to_json
  end
end

```

```node
app.get('/api/accounts', async function (request, response, next) {
  try {
    const accountsResponse = await client.accountsGet({
      access_token: accessToken,
    });
    prettyPrintResponse(accountsResponse);
    response.json(accountsResponse.data);
  } catch (error) {
    prettyPrintResponse(error);
    return response.json(formatError(error.response));
  }
});

```

```go
func accounts(c *gin.Context) {
  accountsGetResp, _, err := client.PlaidApi.AccountsGet(ctx).AccountsGetRequest(
        *plaid.NewAccountsGetRequest(accessToken),
    ).Execute()

  if err != nil {
    c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
    return
  }

  c.JSON(http.StatusOK, gin.H{
    "accounts": accountsGetResp.GetAccounts(),
  })
}

```

```java
@Path("/accounts")
@Produces(MediaType.APPLICATION_JSON)
public class AccountsResource {
  private PlaidClient plaidClient;

  public AccountsResource(PlaidClient plaidClient) {
    this.plaidClient = plaidClient;
  }

  @GET
  public AccountsGetResponse getAccounts() throws IOException {
    Response accountsResponse = plaidClient.service()
      .accountsGet(new AccountsGetRequest(QuickstartApplication.accessToken))
      .execute();

    return accountsResponse.body();
  }
}

```

Example response data:

/accounts/get response

```json
{
  "accounts": [
    {
      "account_id": "A3wenK5EQRfKlnxlBbVXtPw9gyazDWu1EdaZD",
      "balances": {
        "available": 100,
        "current": 110,
        "iso_currency_code": "USD",
        "limit": null,
        "unofficial_currency_code": null
      },
      "mask": "0000",
      "name": "Plaid Checking",
      "official_name": "Plaid Gold Standard 0% Interest Checking",
      "subtype": "checking",
      "type": "depository"
    },
    {
      "account_id": "GPnpQdbD35uKdxndAwmbt6aRXryj4AC1yQqmd",
      "balances": {
        "available": 200,
        "current": 210,
        "iso_currency_code": "USD",
        "limit": null,
        "unofficial_currency_code": null
      },
      "mask": "1111",
      "name": "Plaid Saving",
      "official_name": "Plaid Silver Standard 0.1% Interest Saving",
      "subtype": "savings",
      "type": "depository"
    },
    {
      "account_id": "nVRK5AmnpzFGv6LvpEoRivjk9p7N16F6wnZrX",
      "balances": {
        "available": null,
        "current": 1000,
        "iso_currency_code": "USD",
        "limit": null,
        "unofficial_currency_code": null
      },
      "mask": "2222",
      "name": "Plaid CD",
      "official_name": "Plaid Bronze Standard 0.2% Interest CD",
      "subtype": "cd",
      "type": "depository"
    },
    ...
  ],
  "item": {
    "available_products": [
      "assets",
      "balance",
      "identity",
      "investments",
      "transactions"
    ],
    "billed_products": ["auth"],
    "consent_expiration_time": null,
    "error": null,
    "institution_id": "ins_12",
    "item_id": "gVM8b7wWA5FEVkjVom3ri7oRXGG4mPIgNNrBy",
    "webhook": "https://requestb.in"
  },
  "request_id": "C3IZlexgvNTSukt"
}
```

#### Next steps 

Congratulations, you have completed the Plaid Quickstart! From here, we invite you to modify the Quickstart code in order to get more practice with the Plaid API. There are a few directions you can go in now:

Go to the [docs homepage](https://plaid.com/docs/index.html.md) for links to product-specific documentation.

For more sample apps, including a bare-bones minimal Plaid Quickstart implementation and apps demonstrating real world examples of funds transfer and personal financial management, see [sample apps](https://plaid.com/docs/resources/index.html.md#sample-apps) .

Our YouTube playlist [Plaid in 3 minutes](https://www.youtube.com/playlist?list=PLyKH4ZiEQ1bE7XBcpX81BQWLy1olem1wf) has brief introductions to many Plaid products. For more detailed tutorial videos, see [Plaid Academy](https://www.youtube.com/playlist?list=PLyKH4ZiEQ1bH5wpCt9SiyVfHlV2HecFBq) .

Looking to move money with a Plaid partner, such as [Dwolla](https://plaid.com/docs/auth/partnerships/dwolla/index.html.md) ? See [Move money with our partners](https://plaid.com/docs/auth/partnerships/index.html.md) for partner-specific money movement Quickstarts.

The Quickstart covers working with web apps. If your Plaid app will be on mobile, see [Plaid Link](https://plaid.com/docs/link/index.html.md) to learn about getting started with mobile client-side setup.

Need help?

If you'd like to integrate with Plaid and don't have in-house technical resources, [contact a Plaid integration partner](https://plaid.com/docs/resources/index.html.md#integration-support) to build a Plaid integration for you.