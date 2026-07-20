# Docker-compose information for the deployment of Elasticsearch + Kibana
Deploy ES + K using `docker-compose up -d`

> ⚠️ **Warning:** Elasticsearch is not currently attached to the same Docker network as Atenea.  
> As a result, you cannot rely on container name resolution to reach Elasticsearch. Instead, you must connect using the Elasticsearch server’s **IP address** and **port number** (e.g., `http://192.168.1.100:9200`) when configuring Atenea’s Elasticsearch settings.  
> 
> If you attempt to use a hostname (for example, `elasticsearch:9200`) without being on the same network, Atenea will not be able to locate the cluster. To avoid connection errors, always specify the full `IP:PORT` until both services share a network.  

> ℹ️ **Note:** You can view the available Elasticsearch Docker image versions for the `STACK_VERSION` variable here:  
> https://hub.docker.com/_/elasticsearch/tags  
> Pick the tag that best fits your compatibility requirements.

## Cert folder

To use the ES Cluster for Atenea, ask an administrator for the public key of the Elasticsearch cluster. This `ca.crt` file should be stored in the `es_cluster/certs` folder.

To copy the certificate from the running container into that folder, run:

```bash
docker exec -it es_cluster_es01_1 \
  cat /usr/share/elasticsearch/config/certs/ca/ca.crt \
  > es_cluster/certs/ca.crt
```
After this, the `es_cluster/certs/ca.crt` file will be available for Atenea to establish a secure connection.


### How to Renew the Certificates (If Expired)

If the cluster certificates expire (this usually happens when the root CA reaches its 3-year limit), you must regenerate the entire chain (CA + Node Certificates) in PEM format, apply them to the shared volume, and restart the cluster. Follow these 4 steps:

#### 1. Generate and apply a NEW Certificate Authority (CA)

First, create a new CA and unzip it directly into the shared volume to overwrite the expired one:

```bash
docker exec -it es_cluster_es01_1 /usr/share/elasticsearch/bin/elasticsearch-certutil ca --pem --out /usr/share/elasticsearch/new-ca.zip
# Press Enter to leave passwords blank if asked.

docker exec -it --user root es_cluster_es01_1 unzip -o /usr/share/elasticsearch/new-ca.zip -d /usr/share/elasticsearch/config/certs/

```

#### 2. Generate and apply the NEW Node Certificates

Next, create new certificates for the cluster nodes signed by the new CA:

```bash
docker exec -it es_cluster_es01_1 /usr/share/elasticsearch/bin/elasticsearch-certutil cert --ca-cert /usr/share/elasticsearch/config/certs/ca/ca.crt --ca-key /usr/share/elasticsearch/config/certs/ca/ca.key --multiple --pem --out /usr/share/elasticsearch/new-certs.zip

```

> **⚠️ Important Prompt Instructions:**
> * **Instance name:** Enter `es01` (then repeat for `es02`, `es03`).
> * **IP addresses:** Leave blank (Press Enter).
> * **DNS names:** Enter the docker service name and localhost (e.g., `es01,localhost`, `es02,localhost`).
> * **Passwords:** Leave blank (Press Enter).
> 
> 

Once finished, unzip them into the shared volume:

```bash
docker exec -it --user root es_cluster_es01_1 unzip -o /usr/share/elasticsearch/new-certs.zip -d /usr/share/elasticsearch/config/certs/

```

#### 3. Extract the new CA for external applications

Since the root CA has changed, you must extract it to your local machine so external tools (like Atenea) can trust the cluster again:

```bash
docker exec es_cluster_es01_1 cat /usr/share/elasticsearch/config/certs/ca/ca.crt > es_cluster/certs/ca.crt

```

#### 4. Hard Restart the Cluster

To force Elasticsearch and Kibana to release the old memory cache and load the new certificates, perform a hard stop and start:

```bash
docker restart es_cluster_es01_1 es_cluster_es02_1 es_cluster_es03_1 es_cluster_kibana_1

# Also restart Atenea cluster

```


## Obtaining an Elasticsearch API Key

1. In Kibana, go to **Stack Management > Security > API Keys**.
2. Click **Create API key** and fill in any required details.
3. After creation, switch to the **Beat or Logstash** format. This will present the key as `<id>:<api_key>`.  
   Example output: `l9cPNZcBqAtOa2DZeNrV:oxNY2gvbROO0R5kGzwuwxg`
4. Copy the two parts (before and after the colon) into your environment:
```bash
ELASTICSEARCH_APIKEY_ID=l9cPNZcBqAtOa2DZeNrV
ELASTICSEARCH_APIKEY_API_KEY=oxNY2gvbROO0R5kGzwuwxg
```

Atenea currently requires these separate variables in order to authenticate.

> ℹ️ **Note:** There is a TODO to update Atenea so it can accept a single encoded API key instead of splitting it into two values.

## plugins.sh

Is a script to install in the cluster any required plugin.

**Required plugins**:
- None
