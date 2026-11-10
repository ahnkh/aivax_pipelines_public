
from lib_include import *

'''
전역 모듈의 로딩
기존의 main.py에 있던 전역 데이터 할당 함수, 위치를 이동한다.

TODO: 이 기능에 대해서는, 향후 리펙토링
'''

PIPELINES = {}
PIPELINE_MODULES = {}
PIPELINE_NAMES = {}

async def on_startup():
    
    #TODO: 동기로 변경해본다.
    # await load_modules_from_directory(PIPELINES_DIR)
    load_modules_from_directory(PIPELINES_DIR)

    for module in PIPELINE_MODULES.values():
        if hasattr(module, "on_startup"):
            await module.on_startup()

async def on_shutdown():
    for module in PIPELINE_MODULES.values():
        if hasattr(module, "on_shutdown"):
            await module.on_shutdown()

async def reload():
    await on_shutdown()
    # Clear existing pipelines
    PIPELINES.clear()
    PIPELINE_MODULES.clear()
    PIPELINE_NAMES.clear()
    # Load pipelines afresh
    await on_startup()


def get_all_pipelines() -> dict:
    
    pipelines = {}
    for pipeline_id in PIPELINE_MODULES.keys():
        pipeline = PIPELINE_MODULES[pipeline_id]

        if hasattr(pipeline, "type"):
            if pipeline.type == "manifold":
                manifold_pipelines = []

                # Check if pipelines is a function or a list
                if callable(pipeline.pipelines):
                    manifold_pipelines = pipeline.pipelines()
                else:
                    manifold_pipelines = pipeline.pipelines

                for p in manifold_pipelines:
                    manifold_pipeline_id = f'{pipeline_id}.{p["id"]}'

                    manifold_pipeline_name = p["name"]
                    if hasattr(pipeline, "name"):
                        manifold_pipeline_name = (
                            f"{pipeline.name}{manifold_pipeline_name}"
                        )

                    pipelines[manifold_pipeline_id] = {
                        "module": pipeline_id,
                        "type": pipeline.type if hasattr(pipeline, "type") else "pipe",
                        "id": manifold_pipeline_id,
                        "name": manifold_pipeline_name,
                        "valves": (
                            pipeline.valves if hasattr(pipeline, "valves") else None
                        ),
                    }
            if pipeline.type == "filter":
                pipelines[pipeline_id] = {
                    "module": pipeline_id,
                    "type": (pipeline.type if hasattr(pipeline, "type") else "pipe"),
                    "id": pipeline_id,
                    "name": (
                        pipeline.name if hasattr(pipeline, "name") else pipeline_id
                    ),
                    "pipelines": (
                        pipeline.valves.pipelines
                        if hasattr(pipeline, "valves")
                        and hasattr(pipeline.valves, "pipelines")
                        else []
                    ),
                    "priority": (
                        pipeline.valves.priority
                        if hasattr(pipeline, "valves")
                        and hasattr(pipeline.valves, "priority")
                        else 0
                    ),
                    "valves": pipeline.valves if hasattr(pipeline, "valves") else None,
                }
        else:
            pipelines[pipeline_id] = {
                "module": pipeline_id,
                "type": (pipeline.type if hasattr(pipeline, "type") else "pipe"),
                "id": pipeline_id,
                "name": (pipeline.name if hasattr(pipeline, "name") else pipeline_id),
                "valves": pipeline.valves if hasattr(pipeline, "valves") else None,
            }

    return pipelines


def install_frontmatter_requirements(requirements):
    
    if requirements:
        req_list = [req.strip() for req in requirements.split(",")]
        for req in req_list:
            print(f"Installing requirement: {req}")
            subprocess.check_call([sys.executable, "-m", "pip", "install", req])
    else:
        print("No requirements found in frontmatter.")


# async def load_module_from_path(module_name, module_path):
def load_module_from_path(module_name, module_path):

    try:
        # Read the module content
        with open(module_path, "r") as file:
            content = file.read()

        # Parse frontmatter
        frontmatter = {}
        if content.startswith('"""'):
            end = content.find('"""', 3)
            if end != -1:
                frontmatter_content = content[3:end]
                frontmatter = parse_frontmatter(frontmatter_content)

        # Install requirements if specified
        if "requirements" in frontmatter:
            install_frontmatter_requirements(frontmatter["requirements"])

        # Load the module
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print(f"Loaded module: {module.__name__}")
        if hasattr(module, "Pipeline"):
            return module.Pipeline()
        else:
            raise Exception("No Pipeline class found")
    except Exception as e:
        print(f"Error loading module: {module_name}")

        # Move the file to the error folder
        failed_pipelines_folder = os.path.join(PIPELINES_DIR, "failed")
        if not os.path.exists(failed_pipelines_folder):
            os.makedirs(failed_pipelines_folder)

        failed_file_path = os.path.join(failed_pipelines_folder, f"{module_name}.py")
        os.rename(module_path, failed_file_path)
        print(e)
    return None


# 동기 구조로 변경
# async def load_modules_from_directory(directory):
def load_modules_from_directory(directory:str):
    
    '''
    '''
    
    global PIPELINE_MODULES
    global PIPELINE_NAMES

    for filename in os.listdir(directory):
        if filename.endswith(".py"):
            module_name = filename[:-3]  # Remove the .py extension
            module_path = os.path.join(directory, filename)

            # Create subfolder matching the filename without the .py extension
            subfolder_path = os.path.join(directory, module_name)
            if not os.path.exists(subfolder_path):
                os.makedirs(subfolder_path)
                logging.info(f"Created subfolder: {subfolder_path}")

            # Create a valves.json file if it doesn't exist
            valves_json_path = os.path.join(subfolder_path, "valves.json")
            if not os.path.exists(valves_json_path):
                with open(valves_json_path, "w") as f:
                    json.dump({}, f)
                logging.info(f"Created valves.json in: {subfolder_path}")

            # pipeline = await load_module_from_path(module_name, module_path)
            pipeline = load_module_from_path(module_name, module_path)
            
            if pipeline:
                # Overwrite pipeline.valves with values from valves.json
                if os.path.exists(valves_json_path):
                    with open(valves_json_path, "r") as f:
                        valves_json = json.load(f)
                        if hasattr(pipeline, "valves"):
                            ValvesModel = pipeline.valves.__class__
                            # Create a ValvesModel instance using default values and overwrite with valves_json
                            combined_valves = {
                                **pipeline.valves.model_dump(),
                                **valves_json,
                            }
                            valves = ValvesModel(**combined_valves)
                            pipeline.valves = valves

                            logging.info(f"Updated valves for module: {module_name}")

                pipeline_id = pipeline.id if hasattr(pipeline, "id") else module_name
                PIPELINE_MODULES[pipeline_id] = pipeline
                PIPELINE_NAMES[pipeline_id] = module_name
                logging.info(f"Loaded module: {module_name}")
            else:
                logging.warning(f"No Pipeline class found in {module_name}")

    global PIPELINES
    PIPELINES = get_all_pipelines()



##################################### 공통 유틸리티 개념 함수들

def parse_frontmatter(content:str):
    frontmatter = {}
    for line in content.split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            frontmatter[key.strip().lower()] = value.strip()
    return frontmatter