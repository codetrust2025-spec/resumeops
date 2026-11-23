"""Role-relevant certifications to recommend: well-known vendor certifications and long-running Udemy courses.

These are recommendations only. A certification is printed on a resume only when the candidate states it, or
ticks it in the review card as one they have completed; nothing here is ever added on its own."""
import re

UDEMY_REACT = 'Udemy: React - The Complete Guide (incl. Next.js, Redux)'
UDEMY_JS = 'Udemy: The Complete JavaScript Course - From Zero to Expert'
UDEMY_TS = 'Udemy: Understanding TypeScript'
UDEMY_NODE = 'Udemy: NodeJS - The Complete Guide (MVC, REST APIs, GraphQL)'
UDEMY_JAVA = 'Udemy: Java Programming Masterclass'
UDEMY_SPRING = 'Udemy: Spring Boot 3, Spring 6 & Hibernate for Beginners'
UDEMY_PYTHON = 'Udemy: 100 Days of Code - The Complete Python Pro Bootcamp'
UDEMY_DOCKER = 'Udemy: Docker & Kubernetes - The Practical Guide'
UDEMY_AWS_SAA = 'Udemy: Ultimate AWS Certified Solutions Architect Associate'
UDEMY_ML = 'Udemy: Machine Learning A-Z'
UDEMY_POWER_BI = 'Udemy: Microsoft Power BI Desktop for Business Intelligence'
META_FRONTEND = 'Meta Front-End Developer Professional Certificate (Coursera)'
ORACLE_JAVA = 'Oracle Certified Professional: Java SE 17 Developer'
SPRING_PRO = 'Spring Certified Professional'
PCAP = 'PCAP - Certified Associate in Python Programming'
JSNAD = 'OpenJS Node.js Application Developer (JSNAD)'
AWS_DEV = 'AWS Certified Developer - Associate'
AWS_SAA = 'AWS Certified Solutions Architect - Associate'
AWS_CP = 'AWS Certified Cloud Practitioner'
AWS_DEVOPS = 'AWS Certified DevOps Engineer - Professional'
AZ_900 = 'Microsoft Certified: Azure Fundamentals (AZ-900)'
AZ_104 = 'Microsoft Certified: Azure Administrator Associate (AZ-104)'
AZ_204 = 'Microsoft Certified: Azure Developer Associate (AZ-204)'
AZ_305 = 'Microsoft Certified: Azure Solutions Architect Expert (AZ-305)'
AZ_400 = 'Microsoft Certified: DevOps Engineer Expert (AZ-400)'
GCP_ACE = 'Google Cloud Associate Cloud Engineer'
GCP_PCA = 'Google Cloud Professional Cloud Architect'
TERRAFORM = 'HashiCorp Certified: Terraform Associate'
CKA = 'Certified Kubernetes Administrator (CKA)'

RECOMMENDED = {
    'ui-developer': [META_FRONTEND, UDEMY_JS, 'Udemy: Advanced CSS and Sass - Flexbox, Grid, Animations', UDEMY_TS],
    'frontend-developer': [META_FRONTEND, UDEMY_REACT, UDEMY_JS, UDEMY_TS],
    'react-developer': [META_FRONTEND, UDEMY_REACT, UDEMY_TS, 'Udemy: React Testing Library and Jest - The Complete Guide'],
    'angular-developer': ['Udemy: Angular - The Complete Guide', UDEMY_TS, META_FRONTEND, UDEMY_JS],
    'java-backend-developer': [ORACLE_JAVA, SPRING_PRO, UDEMY_JAVA, UDEMY_SPRING, AWS_DEV],
    'python-backend-developer': [PCAP, UDEMY_PYTHON, AWS_DEV, UDEMY_DOCKER],
    'nodejs-developer': [JSNAD, UDEMY_NODE, AWS_DEV, UDEMY_DOCKER],
    'java-fullstack-developer': [ORACLE_JAVA, SPRING_PRO, UDEMY_SPRING, UDEMY_REACT, AWS_DEV],
    'mern-stack-developer': ['MongoDB Certified Developer Associate', JSNAD, UDEMY_REACT, UDEMY_NODE],
    'dotnet-developer': [AZ_204, AZ_900, UDEMY_DOCKER],
    'devops-engineer': [CKA, TERRAFORM, AWS_DEVOPS, AZ_400, UDEMY_DOCKER],
    'aws-cloud-engineer': [AWS_SAA, AWS_CP, TERRAFORM, UDEMY_AWS_SAA, CKA],
    'azure-cloud-engineer': [AZ_104, AZ_900, AZ_305, TERRAFORM],
    'gcp-cloud-engineer': [GCP_ACE, GCP_PCA, 'Google Cloud Professional Cloud DevOps Engineer', TERRAFORM],
    'data-engineer': ['Databricks Certified Data Engineer Associate', 'AWS Certified Data Engineer - Associate',
                      'Google Cloud Professional Data Engineer', 'SnowPro Core Certification',
                      'Astronomer Certification for Apache Airflow Fundamentals'],
    'azure-data-engineer': ['Microsoft Certified: Fabric Data Engineer Associate (DP-700)',
                            'Microsoft Certified: Azure Data Fundamentals (DP-900)',
                            'Databricks Certified Data Engineer Associate', AZ_900],
    'gcp-data-engineer': ['Google Cloud Professional Data Engineer', GCP_ACE,
                          'Databricks Certified Data Engineer Associate',
                          'Astronomer Certification for Apache Airflow Fundamentals'],
    'data-analyst': ['Google Data Analytics Professional Certificate',
                     'Microsoft Certified: Power BI Data Analyst Associate (PL-300)', 'Tableau Desktop Specialist',
                     UDEMY_POWER_BI],
    'data-scientist': ['IBM Data Science Professional Certificate',
                       'DeepLearning.AI Machine Learning Specialization (Coursera)',
                       'Microsoft Certified: Azure Data Scientist Associate (DP-100)', UDEMY_ML],
    'ml-engineer': ['AWS Certified Machine Learning Engineer - Associate',
                    'Google Cloud Professional Machine Learning Engineer',
                    'DeepLearning.AI Deep Learning Specialization (Coursera)', UDEMY_ML],
    'generative-ai-engineer': ['AWS Certified AI Practitioner', 'Microsoft Certified: Azure AI Engineer Associate (AI-102)',
                               'DeepLearning.AI Generative AI with Large Language Models (Coursera)',
                               'Google Cloud Professional Machine Learning Engineer'],
    'software-tester': ['ISTQB Certified Tester Foundation Level (CTFL)',
                        'ISTQB Certified Tester Foundation Level Agile Tester (CTFL-AT)',
                        'ISTQB Certified Tester Advanced Level Test Analyst (CTAL-TA)',
                        'Udemy: Postman - The Complete Guide - REST API Testing'],
    'automation-tester': ['ISTQB Certified Tester Foundation Level (CTFL)',
                          'ISTQB Advanced Level Test Automation Engineering (CTAL-TAE)',
                          'Udemy: Selenium WebDriver with Java - Basics to Advanced + Frameworks'],
    'salesforce-developer': ['Salesforce Certified Platform Developer I', 'Salesforce Certified Platform App Builder',
                             'Salesforce Certified Administrator'],
    'mobile-app-developer': ['Udemy: Flutter & Dart - The Complete Guide', 'Udemy: React Native - The Practical Guide',
                             'Meta React Native Specialization (Coursera)'],
    'android-developer': ['Meta Android Developer Professional Certificate (Coursera)',
                          'Android Basics with Compose (Google Developers course)',
                          'Udemy: Flutter & Dart - The Complete Guide'],
    'ios-developer': ['App Development with Swift Certified User (Apple)',
                      'Udemy: iOS & Swift - The Complete iOS App Development Bootcamp',
                      'Meta iOS Developer Professional Certificate (Coursera)'],
    'fullstack-developer': [META_FRONTEND, UDEMY_REACT, UDEMY_NODE, AWS_DEV],
    'software-engineer': [ORACLE_JAVA, PCAP, AWS_DEV, UDEMY_DOCKER],
    'backend-developer': [AWS_DEV, UDEMY_NODE, ORACLE_JAVA, PCAP, UDEMY_DOCKER],
    'cloud-engineer': [AWS_SAA, AZ_104, GCP_ACE, TERRAFORM, CKA],
}

# A custom role: recommendations follow the technologies it names.
BY_KEYWORD = [
    (r'\baws\b|amazon web services', [AWS_SAA, AWS_DEV, AWS_CP]),
    (r'\bazure\b', [AZ_900, AZ_104, AZ_204]),
    (r'\bgcp\b|google cloud|bigquery', [GCP_ACE, 'Google Cloud Professional Data Engineer']),
    (r'kubernetes|docker|devops', [CKA, UDEMY_DOCKER]),
    (r'terraform', [TERRAFORM]),
    (r'\bjava\b(?!script)|spring', [ORACLE_JAVA, SPRING_PRO, UDEMY_JAVA]),
    (r'\bpython\b|django|fastapi|flask', [PCAP, UDEMY_PYTHON]),
    (r'node', [JSNAD, UDEMY_NODE]),
    (r'react', [META_FRONTEND, UDEMY_REACT]),
    (r'angular', ['Udemy: Angular - The Complete Guide']),
    (r'power bi|tableau|analyst', ['Microsoft Certified: Power BI Data Analyst Associate (PL-300)',
                                   'Google Data Analytics Professional Certificate']),
    (r'machine learning|\bml\b|\bai\b|llm', ['DeepLearning.AI Machine Learning Specialization (Coursera)', UDEMY_ML]),
    (r'selenium|test|qa\b', ['ISTQB Certified Tester Foundation Level (CTFL)']),
]


def _key(name):
    return re.sub(r'[^a-z0-9]+', ' ', name.casefold()).strip()


def recommended_certifications(target, have=(), limit=5):
    """Up to ``limit`` certifications worth earning for the selected role, without the ones the candidate already
    lists. Recommendations only: nothing here is printed unless the candidate confirms they completed it."""
    items = list(RECOMMENDED.get(target.get('role')) or [])
    if not items:
        words = ' '.join([str(target.get('role_title') or ''), *map(str, target.get('keywords') or [])]).casefold()
        for pattern, names in BY_KEYWORD:
            if re.search(pattern, words):
                items.extend(names)
    owned = [_key(item) for item in have or []]
    out = []
    for name in dict.fromkeys(items):
        key = _key(name)
        if not any(key in other or other in key for other in owned if len(other) > 6):
            out.append(name)
    return out[:limit]
