from uuid import uuid4
from requests.exceptions import ConnectionError
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.orm import Session

from app.apis.RequestStrategy import RequestContext
from app.crud import add_token, cleanup_expired_tokens
from app.dependencies import authenticate_user, create_access_token, pwd_context, create_refresh_token
from app.dependencies import get_db_session, get_request_strategy, get_payload_from_refresh_token, get_current_user
from app.helpers.helper import get_header
from app.models.models import User
from app.routes.wallet import payment_processor
from app.schemas.schemas import UserLoginSchema, UserRegistrationSchema, UserSocialLoginSchema
from app.schemas.user.schema import (LoginResponseModel, RegisterResponseModel, UserDetailResponseModel,
                                     UserDetailUpdateModel)

router = APIRouter()


@router.post("/login", response_model=LoginResponseModel)
async def login(credentials: UserLoginSchema,
                background_tasks: BackgroundTasks,
                request_strategy: RequestContext = Depends(get_request_strategy),
                db: Session = Depends(get_db_session)):

    response = request_strategy.make_request(endpoint="/token",
                                             method="post",
                                             data=credentials.model_dump())

    user = authenticate_user(credentials.email, credentials.password, db)
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    access_token = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})

    if response.status_code == status.HTTP_200_OK:
        # update the token in the database
        logger.debug(f"Adding token to database: {response.json()}")
        add_token(db=db, user_email=user.email, token=response.json()['access'])
        background_tasks.add_task(cleanup_expired_tokens, db, user.email)
        return JSONResponse({"access_token": access_token,
                             "refresh_token": refresh_token,
                             "token_type": "bearer"})

    logger.error(f"Failed to login in the remote predico server: {response.json()}")
    raise HTTPException(status_code=response.status_code, detail="Failed to login")


@router.post("/social-login", response_model=LoginResponseModel)
async def login(credentials: UserSocialLoginSchema,
                background_tasks: BackgroundTasks,
                request_strategy: RequestContext = Depends(get_request_strategy),
                db_session: Session = Depends(get_db_session)):
    try:

        # Get token
        response = request_strategy.make_request(endpoint="/token/social",
                                                 method="post",
                                                 data=credentials.model_dump())

        if not response.status_code == status.HTTP_201_CREATED:
            logger.error(f"Failed to login in the remote predico server: {response.json()}")
            raise HTTPException(status_code=response.status_code, detail="Failed to login")

        # Update the token in the database
        data = response.json()['data']
        user_email = data["user_email"]
        logger.debug(f"Adding token to database: {data['access']}")

        # Verify if it is registered
        user = db_session.query(User).filter(User.email == user_email).first()
        if not user:
            credentials = {
                'email': user_email,
                'password': uuid4().hex
            }
            # If user does not exist, proceed with registration logic
            hashed_password = pwd_context.hash(credentials['password'])
            new_user = User(email=credentials['email'], password_hash=hashed_password)
            db_session.add(new_user)
            db_session.commit()

            payment_processor.create_account(identifier=credentials['email'])

        add_token(db=db_session, user_email=user_email, token=data['access'])
        access_token = create_access_token(data={"sub": user_email})
        refresh_token = create_refresh_token(data={"sub": user_email})

        background_tasks.add_task(cleanup_expired_tokens, db_session, user_email)
        return JSONResponse({"access_token": access_token,
                             "refresh_token": refresh_token,
                             "token_type": "bearer"})

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/details", response_model=UserDetailResponseModel)
async def get_user_details(user=Depends(get_current_user),
                           request_strategy: RequestContext = Depends(get_request_strategy),
                           db_session: Session = Depends(get_db_session)):

    header = get_header(db=db_session, user_email=user.email)

    try:
        endpoint = '/user/list'
        response = request_strategy.make_request(endpoint=endpoint,
                                                 method="get",
                                                 headers=header)
        return JSONResponse(content=response.json(),
                            status_code=200,
                            media_type="application/json")

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.patch("/details", response_model=UserDetailResponseModel)
async def patch_user_details(update_data: UserDetailUpdateModel,
                             user=Depends(get_current_user),
                             request_strategy: RequestContext = Depends(get_request_strategy),
                             db_session: Session = Depends(get_db_session),):
    header = get_header(db=db_session, user_email=user.email)
    user_data = update_data.model_dump(exclude_unset=True)
    try:
        endpoint = '/user/list'
        response = request_strategy.make_request(endpoint=endpoint,
                                                 method="patch",
                                                 headers=header,
                                                 json=user_data)

        return JSONResponse(content=response.json(),
                            status_code=200,
                            media_type="application/json")

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/refresh", response_model=LoginResponseModel)
async def post_refresh_token(refresh_token: str, db: Session = Depends(get_db_session)):
    email = get_payload_from_refresh_token(refresh_token=refresh_token)
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    new_access_token = create_access_token(data={"sub": email})
    new_refresh_token = create_refresh_token(data={"sub": email})

    return {"access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "bearer"}


@router.post("/register", response_model=RegisterResponseModel)
def register_user(credentials: UserRegistrationSchema,
                  request_strategy: RequestContext = Depends(get_request_strategy),
                  db: Session = Depends(get_db_session)):
    try:
        response = request_strategy.make_request(data=credentials.model_dump(),
                                                 endpoint="/user/register/",
                                                 method="post")

    except ConnectionError as e:
        return JSONResponse(content={"error": str(e)}, status_code=503)

    # Directly return the remote APIs response
    content = response.json()  # Assuming the remote API returns JSON
    status_code = response.status_code

    if (status_code == status.HTTP_201_CREATED) or (status_code == status.HTTP_409_CONFLICT):
        # noinspection PyTypeChecker
        user = db.query(User).filter(User.email == credentials.email).first()
        if not user:
            # If user does not exist, proceed with registration logic
            # Here you would hash the password, create a new User object, add to the session, and commit.
            # For example:
            hashed_password = pwd_context.hash(credentials.password)
            new_user = User(email=credentials.email, password_hash=hashed_password)
            db.add(new_user)
            db.commit()

        try:
            payment_processor.create_account(identifier=credentials.email)
        except Exception as e:
            status_code = status.HTTP_400_BAD_REQUEST
            content = {"error": str(e)}
            logger.error(str(e))
            # Request funds from the faucet for each account

        return JSONResponse(content=content,
                            status_code=status.HTTP_201_CREATED,
                            media_type="application/json")
    else:
        raise HTTPException(status_code=status_code, detail=content)
