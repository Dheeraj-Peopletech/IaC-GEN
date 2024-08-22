import json
import logging
import base64
import os
import boto3
from botocore.exceptions import ClientError
import requests

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize Boto3 clients
s3_client = boto3.client('s3')
codecommit = boto3.client('codecommit')
bedrock = boto3.client(service_name='bedrock-runtime')
bedrock_client = boto3.client(service_name='bedrock-agent-runtime')
model_id = "anthropic.claude-3-sonnet-20240229-v1:0"

def invoke_bedrock_model(prompt, bucket_name, bucket_object, final_draft):
    try:
        # Fetch diagram from S3
        response = s3_client.get_object(Bucket=bucket_name, Key=bucket_object)
        diagram_bytes = response['Body'].read()
    except Exception as e:
        print(f"Error fetching image from S3: {e}")
        return {'statusCode': 500, 'body': json.dumps(f"Error fetching image from S3: {e}")}

    # Encode the image in base64
    encoded_diagram = base64.b64encode(diagram_bytes).decode("utf-8")

    # Prepare the payload for invoking the Claude-3 model
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "temperature": 0,
        "top_k": 250,
        "top_p": 1,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": encoded_diagram,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    })

    # Invoke the Claude-3 model
    try:
        response = bedrock.invoke_model(
            modelId=model_id,
            body=body,
            accept="*/*",
            contentType="application/json"
        )
        
        # Decode response body from bytes to string and parse to JSON
        response_body = json.loads(response.get('body').read())
        # Return generated Terraform code
        tf_code = response_body['content'][0]['text']
        return tf_code

    except Exception as e:
        print(f"Error invoking Claude-3 model")
    return {'statusCode': 500, 'body': json.dumps(f"Error invoking Claude-3 model")}  

def push_to_codecommit(repo_name, branch_name, file_content, file_path):
    try:
        # Commit the generated Terraform code to CodeCommit
        response = codecommit.put_file(
            repositoryName=repo_name,
            branchName=branch_name,
            fileContent=file_content.encode('utf-8'),
            filePath=file_path,
            commitMessage='Add generated Terraform code'
        )
        return response
    except ClientError as e:
        logger.error(f"Failed to commit file to CodeCommit: {e}")
        raise

def retrieve_module_definitions(knowledge_base_id, model_arn):
    query_text = "Retrieve Terraform module sources for AWS services"
    try:
        response = bedrock_client.retrieve_and_generate(
            input={'text': query_text},
            retrieveAndGenerateConfiguration={
                'type': 'KNOWLEDGE_BASE',
                'knowledgeBaseConfiguration': {
                    'knowledgeBaseId': knowledge_base_id,
                    'modelArn': model_arn
                }
            }
        )
        
        # Extracting the text from the response
        response_text = response['output']['text']
        print("KB Response:", response_text)

        # Assuming the response text contains a JSON string with module definitions
        module_definitions = response_text  # Assuming it's JSON; otherwise, json.loads may be needed
        return module_definitions

    except ClientError as e:
        print("An error occurred:", e)
        return {}
    except json.JSONDecodeError as json_err:
        print("JSON parsing error:", json_err)
        return {}

def lambda_handler(event, context):
    print("Received event: " + json.dumps(event))
    try:
        properties = {prop["name"]: prop["value"] for prop in event["requestBody"]["content"]["application/json"]["properties"]}
        bucket_name = properties['diagramS3Bucket']
        bucket_object = properties['diagramS3Key']
        final_draft = properties['final_draft'] 

        # CodeCommit information
        repo_name = 'terraform-iac-repo'  # Replace with your CodeCommit repo name
        branch_name = 'main'  # Replace with the branch name you want to commit to
        file_path = 'main.tf'  # The path where the Terraform file will be stored in the repo

        #Knowledge base ID
        kb_id = os.environ['KNOWLEDGE_BASE_ID']

        # Generate Terraform config using Bedrock model
        module_definitions = retrieve_module_definitions(kb_id, "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-v2")

        # Construct the prompt with module definitions
        terraform_prompt = f"Please analyze the architecture diagram in order to create Infrastrucute-As-A-code. Please use {final_draft} and create the necessary IaC in Terraform: "
        terraform_prompt += ". Use the following module definitions whereever applicable: "
        terraform_prompt += json.dumps(module_definitions)
        terraform_prompt += " Give only Terraform code as the output response."

        # Invoke the model to generate Terraform configuration based on the prompt
        main_tf_content = invoke_bedrock_model(terraform_prompt, bucket_name, bucket_object, final_draft)
        
        # Commit the generated Terraform code to CodeCommit
        push_to_codecommit(repo_name, branch_name, main_tf_content, file_path)
    
        return {
            'messageVersion': '1.0',
            'response': {
                'actionGroup': event['actionGroup'],
                'apiPath': event['apiPath'],
                'httpMethod': event['httpMethod'],
                'httpStatusCode': 200,
                'responseBody': {
                    'application/json': {
                        'body': json.dumps({
                            "message": "Terraform code updated successfully",
                            "filePath": file_path
                        })
                    }
                },
                'sessionAttributes': event.get('sessionAttributes', {}),
                'promptSessionAttributes': event.get('promptSessionAttributes', {})
            }
        }

    except Exception as e:
        logger.error(f"An error occurred: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json'
            },
            'body': json.dumps({
                "error": "An error occurred during the process.",
                "details": str(e)
            })
        }
